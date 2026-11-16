"""Runs the Foundry agents, executes Business Central function tools locally, enforces guardrails,
routes every failed order to human triage, and writes a full audit record (incl. lineage JSON) to SQLite."""
import json
import math
import os
import re
import sqlite3
import time
import uuid
from datetime import datetime, timezone

from azure.identity import DefaultAzureCredential
from azure.ai.projects import AIProjectClient
from opentelemetry import trace
from dotenv import load_dotenv

from lineage import documents_from_run

load_dotenv()

# Telemetry is optional: only configured when a connection string is present.
_conn = os.getenv("APPLICATIONINSIGHTS_CONNECTION_STRING")
if _conn:
    try:
        from azure.monitor.opentelemetry import configure_azure_monitor
        configure_azure_monitor(connection_string=_conn)
    except Exception as e:  # telemetry must never break the app
        print(f"Telemetry setup warning: {e}")
tracer = trace.get_tracer("manufacturing-agent")

DB_PATH = os.getenv("GOVERNANCE_DB", "governance.db")
MAX_ROUNDS = 6  # Foundry function-call runs expire after 10 minutes
CODE_RE = re.compile(r"^[A-Za-z0-9._\-]{1,20}$")
GUARD_PREFIX = "BLOCKED_BY_GUARDRAIL"
SUCCESS_STATUSES = {"SALES_ORDER", "SHORTAGE_PO_RAISED"}

# status -> (owner team, recommended action) for the human triage queue
TRIAGE_RULES = {
    "REJECTED_INVALID_REQUEST": ("Sales Ops", "Clarify the request with the sender, correct it and resubmit."),
    "REJECTED_INVALID_CUSTOMER": ("Customer Master Data",
                                  "Confirm the customer number with the sender. Create the customer in Business "
                                  "Central or reply to the sender, then resubmit the order."),
    "REJECTED_CUSTOMER_BLOCKED": ("Credit Control", "Review why the customer is blocked; unblock or decline the order."),
    "REJECTED_INVALID_ITEM": ("Product Master Data",
                              "Confirm the item number with the sender. Create the item or reply with the correct "
                              "item number, then resubmit."),
    "REJECTED_ITEM_BLOCKED": ("Product Master Data", "Item is blocked: confirm whether it can be sold, or offer a substitute."),
    "REJECTED_ITEM_NOT_STOCKED": ("Product Master Data",
                                  "Item has no tracked inventory (service / non-inventory). Process manually."),
    "BLOCKED_BY_GUARDRAIL": ("IT / Security",
                             "Review the email and the agent's tool calls for unexpected instructions before reprocessing."),
    "NEEDS_REVIEW": ("Sales Ops", "The agent did not finish the required checks. Review the tool log and reprocess manually."),
    "SALES_ORDER_FAILED": ("IT Support", "Business Central failed to create the order. Fix the cause shown in the tool error and reprocess."),
    "SHORTAGE_PO_FAILED": ("Procurement", "Stock is short and no purchase order exists. Check the vendor (pick a valid one in the app "
                           "or fix BC_DEFAULT_VENDOR), then reprocess or raise the PO manually in Business Central."),
    "AGENT_ERROR": ("IT Support", "The agent run crashed (see error). Check Foundry / network / permissions, then reprocess."),
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------- clients ----------------
def get_clients():
    project = AIProjectClient(endpoint=os.environ["FOUNDRY_PROJECT_ENDPOINT"],
                              credential=DefaultAzureCredential())
    return project, project.get_openai_client()


# ---------------- validation helpers (treat model arguments and user input as untrusted) ----------------
def _code(value) -> str:
    value = str(value).strip()
    if not CODE_RE.match(value):
        raise ValueError(f"Invalid code: {value!r}")
    return value


def _qty(value) -> int:
    q = int(value)
    if not 0 < q <= 1_000_000:
        raise ValueError("Quantity out of range")
    return q


def preflight(customer_no, product_code, qty, email_text) -> list:
    """Deterministic checks before any agent or Business Central call. Returns a list of problems."""
    errors = []
    for label, value in (("customer number", customer_no), ("item number", product_code)):
        v = str(value or "").strip()
        if not v:
            errors.append(f"{label} is empty")
        elif not CODE_RE.match(v):
            errors.append(f"{label} {v!r} has an invalid format")
    try:
        q = int(qty)
        if not 0 < q <= 1_000_000:
            errors.append(f"quantity {qty} is outside the allowed range 1 - 1,000,000")
    except (TypeError, ValueError):
        errors.append(f"quantity {qty!r} is not a whole number")
    text = str(email_text or "").strip()
    if not text:
        errors.append("email text is empty")
    elif len(text) > 5000:
        errors.append("email text is longer than 5000 characters")
    return errors


def _blocked(customer_result: dict) -> bool:
    return bool(str((customer_result or {}).get("blocked") or "").strip())


# ---------------- per-run context: verified facts + guardrails ----------------
class RunContext:
    """Holds the declared request and the facts the agents verified in Business Central.
    Write tools are only allowed when the facts support them (defence in depth against LLM mistakes
    and prompt injection in the email)."""

    def __init__(self, customer: str, product: str, qty: int):
        self.customer = customer.strip().upper()
        self.product = product.strip().upper()
        self.qty = int(qty)
        self.facts = {"customer": None, "item": None}

    def record(self, kind: str, code: str, result: dict):
        declared = self.customer if kind == "customer" else self.product
        if code.upper() == declared:  # a lookup of some other code does not verify the declared one
            self.facts[kind] = result

    def stock(self):
        item = self.facts["item"]
        return None if not item or item.get("exists") is not True else (item.get("inventory") or 0)

    def expected_shortage(self):
        stock = self.stock()
        return None if stock is None else max(0, math.ceil(self.qty - stock))

    def _verified_order_basis(self):
        c, i = self.facts["customer"], self.facts["item"]
        if not c or c.get("exists") is not True:
            return "the customer was not verified in Business Central"
        if _blocked(c):
            return "the customer is blocked in Business Central"
        if not i or i.get("exists") is not True:
            return "the item was not verified in Business Central"
        if i.get("blocked"):
            return "the item is blocked in Business Central"
        if i.get("type") not in (None, "", "Inventory"):
            return f"the item type is {i.get('type')!r}, not a stocked inventory item"
        return None

    def guard(self, tool: str, customer=None, product=None, qty=None):
        """Return a reason string when the write must be blocked, else None."""
        if product.upper() != self.product:
            return f"item {product} differs from the declared request ({self.product})"
        basis = self._verified_order_basis()
        if basis:
            return basis
        if tool == "create_sales_order":
            if customer.upper() != self.customer:
                return f"customer {customer} differs from the declared request ({self.customer})"
            if qty != self.qty:
                return f"quantity {qty} differs from the declared request ({self.qty})"
            if qty > self.stock():
                return f"stock {self.stock()} is not enough for quantity {qty}"
        if tool == "create_purchase_order":
            shortage = self.expected_shortage()
            if not shortage:
                return "there is no stock shortage to purchase"
            if qty != shortage:
                return f"purchase quantity {qty} differs from the verified shortage ({shortage})"
        return None


# ---------------- tool execution ----------------
def execute_tool(name: str, args: dict, bc, write_enabled: bool, ctx: RunContext = None, vendor: str = None) -> dict:
    with tracer.start_as_current_span(f"bc_tool.{name}") as span:
        try:
            if name == "check_customer":
                code = _code(args["customer_number"])
                res = bc.check_customer(code)
                if ctx:
                    ctx.record("customer", code, res)
                return res
            if name == "check_inventory":
                code = _code(args["product_code"])
                res = bc.check_inventory(code)
                if ctx:
                    ctx.record("item", code, res)
                return res
            if name == "create_sales_order":
                cust, prod, qty = _code(args["customer_number"]), _code(args["product_code"]), _qty(args["quantity"])
                reason = ctx.guard(name, customer=cust, product=prod, qty=qty) if ctx else None
                if reason:
                    return {"error": f"{GUARD_PREFIX}: {reason}"}
                if not write_enabled:
                    return {"simulated": True, "message": "Dry run: no sales order created in Business Central."}
                return bc.create_sales_order(cust, prod, qty)
            if name == "create_purchase_order":
                prod, qty = _code(args["product_code"]), _qty(args["quantity"])
                reason = ctx.guard(name, product=prod, qty=qty) if ctx else None
                if reason:
                    return {"error": f"{GUARD_PREFIX}: {reason}"}
                # The vendor is chosen by the operator (or BC_DEFAULT_VENDOR), never by the model or the email.
                # It is verified with a read-only call, so a dry run also catches a bad vendor.
                try:
                    vendor_no = bc.resolve_vendor(vendor)
                    v = bc.check_vendor(vendor_no)
                except Exception as e:
                    return {"error": f"VENDOR_NOT_USABLE: {str(e)[:220]} Do not retry; a person must fix this."}
                if not v.get("exists"):
                    return {"error": f"VENDOR_NOT_USABLE: vendor '{vendor_no}' does not exist in Business Central. "
                                     "Do not retry; a person must fix this."}
                if v.get("blocked"):
                    return {"error": f"VENDOR_NOT_USABLE: vendor '{vendor_no}' is blocked ({v['blocked']}). "
                                     "Do not retry; a person must fix this."}
                if not write_enabled:
                    return {"simulated": True, "vendor": vendor_no, "vendor_name": v.get("display_name", ""),
                            "message": "Dry run: no purchase order created in Business Central."}
                res = bc.create_purchase_order(prod, qty, vendor_no)
                res.setdefault("vendor_name", v.get("display_name", ""))
                return res
            return {"error": f"Unknown tool {name}"}
        except Exception as e:
            span.record_exception(e)
            return {"error": str(e)[:300]}


def _is_guard(result) -> bool:
    return isinstance(result, dict) and str(result.get("error", "")).startswith(GUARD_PREFIX)


# ---------------- one agent turn, with the function-calling loop ----------------
class AgentRunError(Exception):
    """Raised when an agent run fails; carries the partial tool log so nothing is lost from the audit trail."""

    def __init__(self, agent, message, tool_log, user_text, ms):
        super().__init__(message)
        self.agent, self.tool_log, self.user_text, self.ms = agent, tool_log, user_text, ms


def run_agent(openai_client, agent_name: str, conversation_id: str, user_text: str, bc,
              write_enabled: bool, ctx: RunContext = None, vendor: str = None) -> dict:
    extra = {"agent_reference": {"name": agent_name, "type": "agent_reference"}}
    tool_log = []
    started = time.time()
    try:
        resp = openai_client.responses.create(input=user_text, conversation=conversation_id, extra_body=extra)
        rounds = 0
        while True:
            calls = [i for i in resp.output if i.type == "function_call"]
            if not calls:
                break
            if rounds >= MAX_ROUNDS:
                raise RuntimeError(f"did not finish within {MAX_ROUNDS} tool rounds")
            rounds += 1
            outputs = []
            for c in calls:
                args = json.loads(c.arguments or "{}")
                t0 = time.time()
                result = execute_tool(c.name, args, bc, write_enabled, ctx, vendor)
                tool_log.append({"agent": agent_name, "tool": c.name, "args": args, "result": result,
                                 "ts": _now(), "ms": int((time.time() - t0) * 1000),
                                 "guardrail": _is_guard(result)})
                outputs.append({"type": "function_call_output", "call_id": c.call_id, "output": json.dumps(result)})
            resp = openai_client.responses.create(input=outputs, conversation=conversation_id, extra_body=extra)
    except Exception as e:
        raise AgentRunError(agent_name, f"{type(e).__name__}: {str(e)[:300]}", tool_log, user_text,
                            int((time.time() - started) * 1000)) from e
    return {"agent": agent_name, "input": user_text, "text": resp.output_text, "tool_log": tool_log,
            "response_id": resp.id, "ms": int((time.time() - started) * 1000)}


# ---------------- deterministic assessment of what the agents did ----------------
def _first_guard_error(tool_log):
    err = next((t["result"]["error"] for t in tool_log if t.get("guardrail")), None)
    return err[len(GUARD_PREFIX):].lstrip(": ") if err else None


def _check_error(tool_log, tool):
    return next((t["result"]["error"] for t in tool_log
                 if t["tool"] == tool and isinstance(t["result"], dict) and "error" in t["result"]), None)


def _assess_sales(ctx: RunContext, sales: dict):
    """Returns (status, detail). status NEEDS_PROCUREMENT means: continue with the Procurement Agent."""
    log = sales["tool_log"]
    guard = _first_guard_error(log)
    if guard:
        return "BLOCKED_BY_GUARDRAIL", guard
    cust, item = ctx.facts["customer"], ctx.facts["item"]
    if cust is None:
        err = _check_error(log, "check_customer")
        return "NEEDS_REVIEW", (f"check_customer failed: {err}" if err else
                                f"the agent did not verify customer {ctx.customer} with check_customer")
    if cust.get("exists") is False:
        return "REJECTED_INVALID_CUSTOMER", ""
    if _blocked(cust):
        return "REJECTED_CUSTOMER_BLOCKED", f"block type: {cust.get('blocked')}"
    if item is None:
        err = _check_error(log, "check_inventory")
        return "NEEDS_REVIEW", (f"check_inventory failed: {err}" if err else
                                f"the agent did not check stock for item {ctx.product} with check_inventory")
    if item.get("exists") is False:
        return "REJECTED_INVALID_ITEM", ""
    if item.get("blocked"):
        return "REJECTED_ITEM_BLOCKED", ""
    if item.get("type") not in (None, "", "Inventory"):
        return "REJECTED_ITEM_NOT_STOCKED", str(item.get("type"))
    if ctx.qty <= ctx.stock():
        orders = [t for t in log if t["tool"] == "create_sales_order"]
        if any("error" not in t["result"] for t in orders):
            return "SALES_ORDER", ""
        if orders:
            return "SALES_ORDER_FAILED", orders[-1]["result"].get("error", "unknown error")
        return "NEEDS_REVIEW", "stock is sufficient but the agent did not create the sales order"
    return "NEEDS_PROCUREMENT", ""


def _assess_procurement(proc: dict):
    log = proc["tool_log"]
    guard = _first_guard_error(log)
    if guard:
        return "BLOCKED_BY_GUARDRAIL", guard
    pos = [t for t in log if t["tool"] == "create_purchase_order"]
    if any("error" not in t["result"] for t in pos):
        return "SHORTAGE_PO_RAISED", ""
    if pos:
        return "SHORTAGE_PO_FAILED", pos[-1]["result"].get("error", "unknown error")
    return "SHORTAGE_PO_FAILED", "the Procurement Agent did not create a purchase order"


def _explain(status: str, detail: str, req: dict) -> str:
    c, p, q = req.get("customer"), req.get("product"), req.get("quantity")
    texts = {
        "REJECTED_INVALID_REQUEST": f"The request failed pre-flight validation: {detail}. No agent was called and Business Central was not contacted.",
        "REJECTED_INVALID_CUSTOMER": f"Customer '{c}' does not exist in Business Central (check_customer returned exists=false). No order was created.",
        "REJECTED_CUSTOMER_BLOCKED": f"Customer '{c}' is blocked in Business Central ({detail}). No order was created.",
        "REJECTED_INVALID_ITEM": f"Item '{p}' does not exist in Business Central (check_inventory returned exists=false). No sales or purchase order was created.",
        "REJECTED_ITEM_BLOCKED": f"Item '{p}' is blocked in Business Central and cannot be sold. No order was created.",
        "REJECTED_ITEM_NOT_STOCKED": f"Item '{p}' has type '{detail}', so Business Central does not track its stock. No order was created.",
        "BLOCKED_BY_GUARDRAIL": f"A guardrail stopped an agent action: {detail}. The blocked action wrote nothing to Business Central.",
        "NEEDS_REVIEW": f"The agent did not complete the required checks: {detail}.",
        "SALES_ORDER_FAILED": f"Stock was sufficient for {q} x '{p}', but Business Central did not create the sales order: {detail}.",
        "SHORTAGE_PO_FAILED": f"Stock is short for {q} x '{p}' and the purchase order was not created: {detail}.",
        "AGENT_ERROR": f"The agent run failed before it finished: {detail}.",
    }
    return texts.get(status, detail or status)


# ---------------- full workflow ----------------
def process_order(openai_client, bc, customer_no: str, product_code: str, qty: int,
                  email_text: str, write_enabled: bool, scenario: str = "", vendor: str = "") -> dict:
    """Never raises for business or agent failures: every outcome is classified, logged and, when it is not a
    success, routed to the human triage queue with a plain-English reason."""
    t_start = time.time()
    run = {"run_id": uuid.uuid4().hex[:12], "started_at": _now(), "scenario": scenario,
           "dry_run": not write_enabled, "conversation_id": None,
           "request": {"customer": str(customer_no).strip(), "product": str(product_code).strip(),
                       "quantity": qty, "email": email_text, "vendor": str(vendor or "").strip()},
           "preflight": None, "sales": None, "procurement": None, "agent_error": None,
           "status": None, "detail": "", "documents": [], "triage": None, "duration_ms": 0}

    with tracer.start_as_current_span("process_order") as span:
        span.set_attribute("order.product", str(product_code))
        span.set_attribute("order.qty", str(qty))
        errors = preflight(customer_no, product_code, qty, email_text)
        run["preflight"] = {"ok": not errors, "errors": errors}
        stage = None
        try:
            if errors:
                status, detail = "REJECTED_INVALID_REQUEST", "; ".join(errors)
            else:
                ctx = RunContext(customer_no, product_code, qty)
                stage = "Orchestrator"
                conv = openai_client.conversations.create()
                run["conversation_id"] = conv.id
                stage = "sales"
                run["sales"] = run_agent(openai_client, "Sales-Intake-Agent", conv.id, email_text, bc,
                                         write_enabled, ctx)
                status, detail = _assess_sales(ctx, run["sales"])
                if status == "NEEDS_PROCUREMENT":
                    shortage = ctx.expected_shortage()
                    prompt = (f"Sales summary: {run['sales']['text']}\n"
                              f"Requested {ctx.qty} units of item {ctx.product}; Sales calculated a shortage of "
                              f"{shortage}. Confirm stock and raise the purchase order for the shortage.")
                    stage = "procurement"
                    run["procurement"] = run_agent(openai_client, "Procurement-Agent", conv.id, prompt, bc,
                                                   write_enabled, ctx, vendor=run["request"]["vendor"] or None)
                    status, detail = _assess_procurement(run["procurement"])
        except AgentRunError as e:
            part = "procurement" if e.agent == "Procurement-Agent" else "sales"
            run[part] = {"agent": e.agent, "input": e.user_text, "text": "", "tool_log": e.tool_log,
                         "response_id": None, "ms": e.ms, "error": str(e)}
            run["agent_error"] = {"agent": e.agent, "message": str(e)}
            status, detail = "AGENT_ERROR", f"{e.agent}: {e}"
        except Exception as e:  # e.g. Foundry authentication or conversation creation failed
            run["agent_error"] = {"agent": stage or "Orchestrator", "message": f"{type(e).__name__}: {str(e)[:300]}"}
            status, detail = "AGENT_ERROR", f"{type(e).__name__}: {str(e)[:300]}"

        run["status"], run["detail"] = status, detail
        run["documents"] = documents_from_run(run)
        if status not in SUCCESS_STATUSES:
            owner, action = TRIAGE_RULES.get(status, ("Sales Ops", "Review the run and reprocess manually."))
            run["triage"] = {"required": True, "reason_code": status,
                             "reason": _explain(status, detail, run["request"]),
                             "owner": owner, "recommended_action": action}
        else:
            run["triage"] = {"required": False}
        run["duration_ms"] = int((time.time() - t_start) * 1000)
        span.set_attribute("order.status", status)
        log_run(run)

    try:
        trace.get_tracer_provider().force_flush()
    except Exception:
        pass
    return run


# ---------------- governance DB (persistent; never dropped; migrated in place) ----------------
_NEW_RUN_COLUMNS = {"run_id": "TEXT", "scenario": "TEXT", "triage_required": "INT",
                    "failure_reason": "TEXT", "duration_ms": "INT", "run_json": "TEXT"}


def _db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS order_runs (
        ts TEXT, request TEXT, customer TEXT, product TEXT, qty INT, status TEXT, dry_run INT,
        sales_output TEXT, procurement_output TEXT, tool_calls TEXT, conversation_id TEXT)""")
    existing = {r[1] for r in conn.execute("PRAGMA table_info(order_runs)")}
    for col, typ in _NEW_RUN_COLUMNS.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE order_runs ADD COLUMN {col} {typ}")
    conn.execute("""CREATE TABLE IF NOT EXISTS triage_cases (
        case_id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, created_ts TEXT, customer TEXT, product TEXT,
        qty INT, reason_code TEXT, reason TEXT, owner TEXT, recommended_action TEXT,
        state TEXT DEFAULT 'OPEN', resolution_action TEXT, resolution_note TEXT, resolved_by TEXT, resolved_ts TEXT)""")
    conn.commit()
    return conn


def _int_or_none(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def log_run(run: dict):
    req, tri = run["request"], run["triage"]
    sales, proc = run.get("sales") or {}, run.get("procurement") or {}
    tools = sales.get("tool_log", []) + proc.get("tool_log", [])
    conn = _db()
    try:
        if tri.get("required"):
            cur = conn.execute(
                """INSERT INTO triage_cases (run_id, created_ts, customer, product, qty, reason_code, reason, owner,
                   recommended_action) VALUES (?,?,?,?,?,?,?,?,?)""",
                (run["run_id"], _now(), req["customer"], req["product"], _int_or_none(req["quantity"]),
                 tri["reason_code"], tri["reason"], tri["owner"], tri["recommended_action"]))
            tri["case_id"] = cur.lastrowid
        conn.execute(
            """INSERT INTO order_runs (ts, request, customer, product, qty, status, dry_run, sales_output,
               procurement_output, tool_calls, conversation_id, run_id, scenario, triage_required,
               failure_reason, duration_ms, run_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (run["started_at"], req["email"], req["customer"], req["product"], _int_or_none(req["quantity"]),
             run["status"], int(run["dry_run"]), sales.get("text", ""), proc.get("text", ""),
             json.dumps(tools, default=str), run["conversation_id"], run["run_id"], run["scenario"],
             int(bool(tri.get("required"))), tri.get("reason", "") if tri.get("required") else "",
             run["duration_ms"], json.dumps(run, default=str)))
        conn.commit()
    finally:
        conn.close()


def read_audit_log():
    import pandas as pd
    conn = _db()
    try:
        return pd.read_sql_query(
            """SELECT ts, run_id, scenario, status, triage_required, customer, product, qty, dry_run,
               failure_reason, duration_ms, conversation_id, request, sales_output, procurement_output, tool_calls
               FROM order_runs ORDER BY ts DESC, rowid DESC""", conn)
    finally:
        conn.close()


def load_run(run_id: str):
    conn = _db()
    try:
        row = conn.execute("SELECT run_json FROM order_runs WHERE run_id = ?", (run_id,)).fetchone()
    finally:
        conn.close()
    return json.loads(row[0]) if row and row[0] else None


def read_triage():
    import pandas as pd
    conn = _db()
    try:
        return pd.read_sql_query("SELECT * FROM triage_cases ORDER BY case_id DESC", conn)
    finally:
        conn.close()


def resolve_triage(case_id: int, action: str, note: str, resolved_by: str):
    conn = _db()
    try:
        conn.execute("""UPDATE triage_cases SET state='RESOLVED', resolution_action=?, resolution_note=?,
                        resolved_by=?, resolved_ts=? WHERE case_id=?""",
                     (action, note, resolved_by, _now(), int(case_id)))
        conn.commit()
    finally:
        conn.close()
