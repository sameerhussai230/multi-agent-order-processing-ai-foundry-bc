"""Builds a left-to-right data-lineage model from one run record (the JSON produced by
orchestrator.process_order) and renders it as Mermaid or Graphviz DOT text.

Pure Python, no Streamlit import: the same function draws a live run, a run replayed from SQLite
and a run loaded from an uploaded JSON file.
"""
import math

# status -> colours (explicit text colour so the diagram stays readable in light and dark themes)
STYLE = {
    "ok": {"fill": "#E6F4EA", "stroke": "#2E7D32"},
    "warn": {"fill": "#FFF4E0", "stroke": "#EF6C00"},
    "error": {"fill": "#FDE7E9", "stroke": "#C62828"},
    "info": {"fill": "#E8F0FE", "stroke": "#1A73E8"},
}
TEXT_COLOR = "#202124"

LANES = [
    ("intake", "Intake & validation"),
    ("sales", "Sales Intake Agent"),
    ("orchestrator", "Orchestrator"),
    ("procurement", "Procurement Agent"),
    ("outcome", "Outcome"),
]

# final status -> (icon, headline, level)
OUTCOMES = {
    "SALES_ORDER": ("✅", "Sales order created", "ok"),
    "SHORTAGE_PO_RAISED": ("📦", "Shortage: purchase order raised", "ok"),
    "REJECTED_INVALID_REQUEST": ("🚫", "Rejected: invalid request", "error"),
    "REJECTED_INVALID_CUSTOMER": ("🚫", "Rejected: unknown customer", "error"),
    "REJECTED_CUSTOMER_BLOCKED": ("🚫", "Rejected: customer blocked", "error"),
    "REJECTED_INVALID_ITEM": ("🚫", "Rejected: unknown item", "error"),
    "REJECTED_ITEM_BLOCKED": ("🚫", "Rejected: item blocked", "error"),
    "REJECTED_ITEM_NOT_STOCKED": ("🚫", "Rejected: item has no tracked stock", "error"),
    "BLOCKED_BY_GUARDRAIL": ("⛔", "Blocked by guardrail", "error"),
    "NEEDS_REVIEW": ("⚠️", "Needs manual review", "warn"),
    "SALES_ORDER_FAILED": ("❌", "Sales order failed", "error"),
    "SHORTAGE_PO_FAILED": ("❌", "Purchase order failed", "error"),
    "AGENT_ERROR": ("❌", "Agent error", "error"),
}


def compact(value, limit: int = 44) -> str:
    """One-line, length-limited text for diagram labels and table cells."""
    if value is None:
        return "-"
    if isinstance(value, dict):
        parts = []
        for k, v in value.items():
            if isinstance(v, list):
                v = f"{len(v)} item(s)" if v else "none"
            elif isinstance(v, dict):
                v = "…"
            parts.append(f"{k}={v}")
        text = ", ".join(parts)
    elif isinstance(value, (list, tuple)):
        text = ", ".join(compact(v, limit) for v in value)
    else:
        text = str(value)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _tool_state(t: dict) -> str:
    r = t.get("result") or {}
    if not isinstance(r, dict):
        return "ok"
    if t.get("guardrail") or str(r.get("error", "")).startswith("BLOCKED_BY_GUARDRAIL"):
        return "error"
    if "error" in r:
        return "error"
    if r.get("simulated"):
        return "warn"
    if r.get("exists") is False:
        return "warn"
    return "ok"


def _first_result(tool_log, tool):
    return next((t.get("result") for t in tool_log or [] if t.get("tool") == tool), None)


def documents_from_run(run: dict) -> list:
    """Business Central documents created (or simulated) by the agents in this run."""
    docs = []
    for part in ("sales", "procurement"):
        for t in (run.get(part) or {}).get("tool_log", []):
            r, tool = t.get("result") or {}, t.get("tool")
            if tool == "create_sales_order" and r.get("created"):
                docs.append({"type": "Sales order", "number": r.get("order_number"), "simulated": False})
            elif tool == "create_sales_order" and r.get("simulated"):
                docs.append({"type": "Sales order", "number": None, "simulated": True})
            elif tool == "create_purchase_order" and r.get("created"):
                docs.append({"type": "Purchase order", "number": r.get("purchase_order_number"), "simulated": False})
            elif tool == "create_purchase_order" and r.get("simulated"):
                docs.append({"type": "Purchase order", "number": None, "simulated": True})
    return docs


def normalize_run(data: dict) -> dict:
    """Make an older / hand-made run JSON (e.g. {status, sales, procurement, dry_run, conversation_id})
    drawable: fill in the keys the lineage view expects without inventing facts."""
    run = dict(data)
    sales = run.get("sales") or {}
    log = sales.get("tool_log", [])
    if not run.get("request"):
        arg = lambda tool, key: next((t.get("args", {}).get(key) for t in log if t.get("tool") == tool), None)
        qty = arg("create_sales_order", "quantity")
        run["request"] = {"customer": arg("check_customer", "customer_number"),
                          "product": arg("check_inventory", "product_code"),
                          "quantity": qty, "email": "(not recorded in this JSON)"}
    for part, agent in (("sales", "Sales-Intake-Agent"), ("procurement", "Procurement-Agent")):
        if run.get(part):
            run[part] = {"agent": agent, "input": None, "ms": None, "tool_log": [], **run[part]}
    run.setdefault("preflight", None)
    run.setdefault("detail", "")
    run.setdefault("started_at", None)
    if "documents" not in run:
        run["documents"] = documents_from_run(run)
    if not run.get("triage"):
        needs = run.get("status") not in ("SALES_ORDER", "SHORTAGE_PO_RAISED")
        run["triage"] = ({"required": True, "reason_code": run.get("status"), "owner": "-",
                          "reason": "Reason was not recorded in this older JSON; see the agent replies and tool calls.",
                          "recommended_action": "Review the run manually."} if needs else {"required": False})
    return run


def describe_outcome(run: dict) -> str:
    """Plain-English sentence for the banner above the diagram."""
    triage = run.get("triage") or {}
    if triage.get("required"):
        return triage.get("reason", "")
    docs = run.get("documents") or []
    parts = []
    for d in docs:
        if d.get("simulated"):
            parts.append(f"{d['type']} (simulated, dry run)")
        else:
            parts.append(f"{d['type']} {d.get('number')}")
    return "Created in Business Central: " + ", ".join(parts) if parts else "Completed."


def build_lineage(run: dict, detail: str = "full") -> dict:
    """Return {"nodes": [...], "edges": [...]} for one run record.
    detail="full" draws every tool call as its own node; detail="overview" groups each agent's tool calls
    into a single node so the diagram stays readable on a projector."""
    req = run.get("request") or {}
    status = run.get("status") or ""
    nodes, edges = [], []

    def add(lane, title, inp=None, out=None, state="ok", ms=None, ts=None, link=None, label=""):
        nid = f"n{len(nodes) + 1}"
        nodes.append({"id": nid, "lane": lane, "title": title, "input": inp, "output": out,
                      "state": state, "ms": ms, "ts": ts})
        if link:
            edges.append({"from": link, "to": nid, "label": label})
        return nid

    # 1) incoming email + declared request
    prev = add("intake", "📧 Customer email", inp=req.get("email"),
               out={"customer": req.get("customer"), "item": req.get("product"), "qty": req.get("quantity")},
               state="info", ts=run.get("started_at"))

    # 2) pre-flight validation
    pre = run.get("preflight")
    if pre is not None:
        prev = add("intake", "🛡️ Pre-flight checks",
                   inp={"customer": req.get("customer"), "item": req.get("product"), "qty": req.get("quantity")},
                   out={"valid": pre.get("ok"), "errors": pre.get("errors") or []},
                   state="ok" if pre.get("ok") else "error", link=prev, label="request")

    def agent_chain(part, lane, title, prev_id, link_label):
        data = run.get(part)
        if not data:
            return prev_id, False
        failed = bool(data.get("error"))
        a = add(lane, title, inp=data.get("input"), out=data.get("text") or data.get("error"),
                state="error" if failed else "ok", ms=data.get("ms"), link=prev_id, label=link_label)
        cur = a
        calls = data.get("tool_log", [])
        if detail == "overview" and calls:
            states = [_tool_state(t) for t in calls]
            bad = states.count("error")
            state = "error" if bad else ("warn" if "warn" in states else "ok")
            cur = add(lane, f"🔧 Business Central tools ({len(calls)})",
                      inp=" → ".join(str(t.get("tool")) for t in calls),
                      out=f"{len(calls) - bad} ok, {bad} failed/blocked", state=state,
                      ms=sum(t.get("ms") or 0 for t in calls), link=cur, label="tool calls")
        else:
            for t in calls:
                cur = add(lane, f"🔧 {t.get('tool')}", inp=t.get("args"), out=t.get("result"),
                          state=_tool_state(t), ms=t.get("ms"), ts=t.get("ts"), link=cur, label="")
        return cur, failed

    # 3) sales agent (+ its tool calls)
    prev, sales_failed = agent_chain("sales", "sales", "🤖 Sales Intake Agent", prev, "email text")

    # 4) orchestrator decision
    sales = run.get("sales")
    if sales and not sales_failed:
        cust = _first_result(sales.get("tool_log"), "check_customer") or {}
        inv = _first_result(sales.get("tool_log"), "check_inventory") or {}
        stock = inv.get("inventory")
        qty = req.get("quantity")
        shortage = None
        if isinstance(stock, (int, float)) and isinstance(qty, (int, float)) and qty > stock:
            shortage = int(math.ceil(qty - stock))
        if run.get("procurement"):
            nxt = f"Stock {stock} < {qty}: shortage {shortage}, call Procurement Agent"
        elif status in ("SALES_ORDER", "SALES_ORDER_FAILED"):
            nxt = f"Stock {stock} >= {qty}: create sales order"
        else:
            nxt = "Stop and route to human triage"
        prev = add("orchestrator", "⚖️ Orchestrator decision",
                   inp={"customer_exists": cust.get("exists"), "item_exists": inv.get("exists"),
                        "stock": stock, "requested": qty},
                   out=nxt, state="info", link=prev, label="tool results")

    # 5) procurement agent (+ its tool calls)
    proc_failed = False
    if run.get("procurement"):
        prev, proc_failed = agent_chain("procurement", "procurement", "🤖 Procurement Agent", prev,
                                        "shortage + sales summary")

    # 6) outcome and triage
    icon, headline, level = OUTCOMES.get(status, ("ℹ️", status or "Unknown", "info"))
    out_id = add("outcome", f"{icon} {headline}", inp=run.get("detail") or None,
                 out={"status": status, "documents": run.get("documents") or [], "dry_run": run.get("dry_run")},
                 state=level, link=prev, label="")
    triage = run.get("triage") or {}
    if triage.get("required"):
        add("outcome", "🧑‍⚖️ Human triage", inp=triage.get("reason"),
            out={"owner": triage.get("owner"), "action": triage.get("recommended_action"),
                 "case_id": triage.get("case_id")},
            state="warn", link=out_id, label="needs a person")
    return {"nodes": nodes, "edges": edges}


def steps_table(model: dict) -> list:
    """Rows for a dataframe: one row per node with compact input/output."""
    lane_title = dict(LANES)
    rows = []
    for i, n in enumerate(model["nodes"], 1):
        rows.append({"#": i, "Actor": lane_title.get(n["lane"], n["lane"]), "Step": n["title"],
                     "Status": {"ok": "OK", "warn": "Attention", "error": "Failed", "info": "Info"}[n["state"]],
                     "Input": compact(n["input"], 70), "Output": compact(n["output"], 70),
                     "ms": n["ms"]})
    return rows


# ------------------------------------------------------------------ Mermaid
def _mm(text, limit: int) -> str:
    s = compact(text, limit)
    for a, b in ((";", ","), ('"', "'"), ("`", "'"), ("|", "/"), ("<", "#lt;"), (">", "#gt;")):
        s = s.replace(a, b)
    return s


def to_mermaid(model: dict) -> str:
    lines = ["flowchart LR"]
    present = {n["lane"] for n in model["nodes"]}
    for lane, title in LANES:
        if lane not in present:
            continue
        lines.append(f'  subgraph lane_{lane}["{title}"]')
        for n in model["nodes"]:
            if n["lane"] == lane:
                label = (f'<b>{_mm(n["title"], 34)}</b><br/>IN: {_mm(n["input"], 30)}'
                         f'<br/>OUT: {_mm(n["output"], 30)}')
                lines.append(f'    {n["id"]}["{label}"]')
        lines.append("  end")
    for e in model["edges"]:
        if e["label"]:
            lines.append(f'  {e["from"]} -->|"{_mm(e["label"], 24)}"| {e["to"]}')
        else:
            lines.append(f'  {e["from"]} --> {e["to"]}')
    for state, c in STYLE.items():
        lines.append(f"  classDef {state} fill:{c['fill']},stroke:{c['stroke']},stroke-width:2px,color:{TEXT_COLOR}")
    for state in STYLE:
        ids = [n["id"] for n in model["nodes"] if n["state"] == state]
        if ids:
            lines.append(f"  class {','.join(ids)} {state}")
    return "\n".join(lines)


# ------------------------------------------------------------------ Graphviz DOT (fallback)
def _dot(text, limit: int) -> str:
    return compact(text, limit).replace("\\", "/").replace('"', "'")


def to_dot(model: dict) -> str:
    out = ["digraph G {", '  rankdir=LR; bgcolor="transparent"; compound=true;',
           '  node [shape=box, style="rounded,filled", fontname="Helvetica", fontsize=11, margin="0.15,0.08"];',
           '  edge [color="#5f6368", fontname="Helvetica", fontsize=9, fontcolor="#5f6368"];']
    present = {n["lane"] for n in model["nodes"]}
    for lane, title in LANES:
        if lane not in present:
            continue
        out.append(f'  subgraph cluster_{lane} {{ label="{title}"; style="rounded,dashed"; '
                   f'color="#9aa0a6"; fontcolor="#9aa0a6"; fontname="Helvetica";')
        for n in model["nodes"]:
            if n["lane"] == lane:
                c = STYLE[n["state"]]
                label = f'{_dot(n["title"], 34)}\\nIN: {_dot(n["input"], 30)}\\nOUT: {_dot(n["output"], 30)}'
                out.append(f'    {n["id"]} [label="{label}", fillcolor="{c["fill"]}", color="{c["stroke"]}", '
                           f'fontcolor="{TEXT_COLOR}", penwidth=2];')
        out.append("  }")
    for e in model["edges"]:
        lab = f' [label="{_dot(e["label"], 24)}"]' if e["label"] else ""
        out.append(f'  {e["from"]} -> {e["to"]}{lab};')
    out.append("}")
    return "\n".join(out)
