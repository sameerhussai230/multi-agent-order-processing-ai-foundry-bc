"""Business Central tools exposed as a remote MCP server on Azure Functions.
Endpoint after deploy: https://<function-app>.azurewebsites.net/runtime/webhooks/mcp
Every call is logged as 'BC_TOOL ...' (tool, args, result, ms) and 'BC_API ...' (HTTP calls to BC)
so it shows up in Application Insights.
"""
import json
import logging
import os
import re
import time

import azure.functions as func
from bc_integration import BusinessCentralAPI

app = func.FunctionApp()
log = logging.getLogger("bc_mcp")

CODE_RE = re.compile(r"^[A-Za-z0-9._\-]{1,20}$")
_bc = None


def bc() -> BusinessCentralAPI:
    global _bc
    if _bc is None:
        _bc = BusinessCentralAPI()
    return _bc


def write_enabled() -> bool:
    # Flip in Azure portal: Function App > Environment variables > BC_WRITE_ENABLED
    return os.getenv("BC_WRITE_ENABLED", "false").strip().lower() == "true"


def _code(v) -> str:
    v = str(v).strip()
    if not CODE_RE.match(v):
        raise ValueError(f"Invalid code: {v!r}")
    return v


def _qty(v) -> int:
    q = int(v)
    if not 0 < q <= 1_000_000:
        raise ValueError("Quantity out of range")
    return q


def _props(*items) -> str:
    return json.dumps([{"propertyName": n, "propertyType": t, "description": d} for n, t, d in items])


def _run(tool: str, context, fn) -> str:
    started = time.time()
    args = {}
    try:
        args = json.loads(context).get("arguments", {})
        result = fn(args)
    except Exception as e:  # return errors to the agent instead of crashing the tool call
        result = {"error": str(e)[:300]}
    log.info("BC_TOOL %s", json.dumps({"tool": tool, "args": args, "result": result,
                                       "ms": int((time.time() - started) * 1000),
                                       "write_enabled": write_enabled()}))
    return json.dumps(result)


CUSTOMER = ("customer_number", "string", "Business Central customer number, e.g. 40000.")
PRODUCT = ("product_code", "string", "Business Central item number, e.g. 1908-S.")
QTY = ("quantity", "integer", "Quantity in units, greater than 0.")


@app.generic_trigger(arg_name="context", type="mcpToolTrigger", toolName="check_customer",
                     description="Checks whether a customer number exists in Business Central.",
                     toolProperties=_props(CUSTOMER))
def check_customer(context) -> str:
    return _run("check_customer", context, lambda a: bc().check_customer(_code(a["customer_number"])))


@app.generic_trigger(arg_name="context", type="mcpToolTrigger", toolName="check_inventory",
                     description="Returns real-time inventory for an item in Business Central.",
                     toolProperties=_props(PRODUCT))
def check_inventory(context) -> str:
    return _run("check_inventory", context, lambda a: bc().check_inventory(_code(a["product_code"])))


@app.generic_trigger(arg_name="context", type="mcpToolTrigger", toolName="create_sales_order",
                     description="Creates a sales order in Business Central. Call only when stock is sufficient.",
                     toolProperties=_props(CUSTOMER, PRODUCT, QTY))
def create_sales_order(context) -> str:
    def go(a):
        c, p, q = _code(a["customer_number"]), _code(a["product_code"]), _qty(a["quantity"])
        if not write_enabled():
            return {"simulated": True, "message": "Dry run: BC_WRITE_ENABLED is not true, no order created."}
        return bc().create_sales_order(c, p, q)
    return _run("create_sales_order", context, go)


@app.generic_trigger(arg_name="context", type="mcpToolTrigger", toolName="create_purchase_order",
                     description="Creates a purchase order in Business Central to cover a stock shortage.",
                     toolProperties=_props(PRODUCT, QTY))
def create_purchase_order(context) -> str:
    def go(a):
        p, q = _code(a["product_code"]), _qty(a["quantity"])
        if not write_enabled():
            return {"simulated": True, "message": "Dry run: BC_WRITE_ENABLED is not true, no PO created."}
        return bc().create_purchase_order(p, q)
    return _run("create_purchase_order", context, go)
