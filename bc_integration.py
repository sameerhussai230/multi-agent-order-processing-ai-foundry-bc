import json
import logging
import os
import re
import time
import msal
import requests
from urllib.parse import quote

log = logging.getLogger("bc_api")
GUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
VENDOR_RE = re.compile(r"^[A-Za-z0-9._\-]{1,20}$")  # Vendor No. is Code[20] in Business Central
API_ROOT = "https://api.businesscentral.dynamics.com/v2.0"
TIMEOUT = 30


def _odata_str(value: str) -> str:
    return str(value).replace("'", "''")


def _blocked_text(value) -> str:
    """BC returns ' ' / '_x0020_' for 'not blocked'. Normalise to '' (not blocked) or the block type."""
    if isinstance(value, bool):
        return "Blocked" if value else ""
    v = "" if value is None else str(value).strip()
    return "" if v in ("", "_x0020_") else v


def _log_call(method: str, path: str, status: int, started: float):
    # Never log tokens or bodies: only method, path, status, latency.
    log.info("BC_API %s", json.dumps({"method": method, "path": path, "status": status,
                                      "ms": int((time.time() - started) * 1000)}))


class BusinessCentralAPI:
    def __init__(self):
        self.tenant_id = os.getenv("BC_TENANT_ID")
        self.client_id = os.getenv("BC_CLIENT_ID", "").strip("{}")
        self.client_secret = os.getenv("BC_CLIENT_SECRET")
        self.environment = os.getenv("BC_ENVIRONMENT", "Production")
        self.company_identifier = os.getenv("BC_COMPANY_ID", "")
        self.default_vendor = os.getenv("BC_DEFAULT_VENDOR", "").strip()
        self.scope = ["https://api.businesscentral.dynamics.com/.default"]
        self._msal = msal.ConfidentialClientApplication(
            self.client_id,
            authority=f"https://login.microsoftonline.com/{self.tenant_id}",
            client_credential=self.client_secret,
        )
        self._base_url = None

    def _token(self) -> str:
        result = self._msal.acquire_token_for_client(scopes=self.scope)
        if "access_token" not in result:
            raise Exception(f"MSAL auth error: {result.get('error_description', result)}")
        return result["access_token"]

    def _headers(self, with_body: bool = False) -> dict:
        h = {"Authorization": f"Bearer {self._token()}", "Accept": "application/json"}
        if with_body:
            h["Content-Type"] = "application/json"
        return h

    def _base(self) -> str:
        if self._base_url:
            return self._base_url
        if GUID_RE.match(self.company_identifier):
            company_id = self.company_identifier
        else:
            t = time.time()
            r = requests.get(f"{API_ROOT}/{self.environment}/api/v2.0/companies",
                             headers=self._headers(), timeout=TIMEOUT)
            _log_call("GET", "/companies", r.status_code, t)
            if r.status_code != 200:
                raise Exception(f"Could not list companies ({r.status_code}): {r.text[:300]}")
            companies = r.json().get("value", [])
            match = next((c for c in companies
                          if self.company_identifier in (c.get("name"), c.get("displayName"))), None)
            if not match:
                raise Exception(f"Company '{self.company_identifier}' not found. "
                                f"Available: {[c.get('name') for c in companies]}")
            company_id = match["id"]
        self._base_url = f"{API_ROOT}/{self.environment}/api/v2.0/companies({company_id})"
        return self._base_url

    def _get(self, path: str, filter_expr: str = None, select: str = None,
             orderby: str = None, top: int = None) -> list:
        params = []
        if filter_expr:
            params.append("$filter=" + quote(filter_expr))
        if select:
            params.append("$select=" + quote(select, safe=","))
        if orderby:
            params.append("$orderby=" + quote(orderby))
        if top:
            params.append(f"$top={int(top)}")
        url = f"{self._base()}/{path}"
        if params:
            url += "?" + "&".join(params)
        t = time.time()
        r = requests.get(url, headers=self._headers(), timeout=TIMEOUT)
        _log_call("GET", f"/{path}", r.status_code, t)
        if r.status_code != 200:
            raise Exception(f"BC API error {r.status_code} at /{path}: {r.text[:300]}")
        return r.json().get("value", [])

    def _post(self, path: str, payload: dict) -> dict:
        t = time.time()
        r = requests.post(f"{self._base()}/{path}", headers=self._headers(True), json=payload, timeout=TIMEOUT)
        _log_call("POST", f"/{path}", r.status_code, t)
        if r.status_code not in (200, 201):
            raise Exception(f"BC API error {r.status_code} at /{path}: {r.text[:300]}")
        return r.json()

    # ---- list reads (used by the Streamlit dropdowns) ----
    def get_customers(self, limit: int = 200) -> list:
        data = self._get("customers", select="number,displayName", orderby="displayName", top=limit)
        return [{"number": c.get("number"), "displayName": c.get("displayName", "")}
                for c in data if c.get("number")]

    def get_items(self, limit: int = 200) -> list:
        data = self._get("items", select="number,displayName,type,inventory,blocked",
                         orderby="displayName", top=limit)
        return [{"number": i.get("number"), "displayName": i.get("displayName", ""),
                 "type": i.get("type", ""), "inventory": i.get("inventory", 0)}
                for i in data if i.get("number") and not i.get("blocked")]

    def get_vendors(self, limit: int = 200) -> list:
        data = self._get("vendors", select="number,displayName,blocked", orderby="displayName", top=limit)
        return [{"number": v.get("number"), "displayName": v.get("displayName", "")}
                for v in data if v.get("number") and not _blocked_text(v.get("blocked"))]

    def default_vendor_is_valid_format(self) -> bool:
        return bool(VENDOR_RE.match(self.default_vendor))

    # ---- single-record reads (used by the agent tools) ----
    def check_vendor(self, vendor_number: str) -> dict:
        data = self._get("vendors", f"number eq '{_odata_str(vendor_number)}'")
        if data:
            return {"exists": True, "vendor_number": data[0].get("number"),
                    "display_name": data[0].get("displayName", ""),
                    "blocked": _blocked_text(data[0].get("blocked"))}
        return {"exists": False}

    def resolve_vendor(self, vendor_number: str = None) -> str:
        """Vendor for a purchase order: the one chosen in the app, else BC_DEFAULT_VENDOR. Validated before any call
        so a placeholder or typo gives a clear message instead of a Business Central 400 error."""
        v = (vendor_number or self.default_vendor or "").strip()
        if not v:
            raise Exception("No vendor selected. Pick a vendor in the app or set BC_DEFAULT_VENDOR to a real vendor number.")
        if not VENDOR_RE.match(v):
            raise Exception(f"'{v[:30]}' is not a valid vendor number (max 20 characters). BC_DEFAULT_VENDOR probably "
                            "still holds the placeholder text; set it to a real vendor number or pick a vendor in the app.")
        return v

    def check_customer(self, customer_number: str) -> dict:
        data = self._get("customers", f"number eq '{_odata_str(customer_number)}'")
        if data:
            return {"exists": True, "customer_number": data[0].get("number"),
                    "display_name": data[0].get("displayName", ""),
                    "blocked": _blocked_text(data[0].get("blocked"))}
        return {"exists": False}

    def check_inventory(self, product_code: str) -> dict:
        data = self._get("items", f"number eq '{_odata_str(product_code)}'")
        if data:
            return {"exists": True, "product_code": data[0].get("number"),
                    "description": data[0].get("displayName", ""),
                    "type": data[0].get("type", ""),
                    "blocked": bool(data[0].get("blocked", False)),
                    "inventory": data[0].get("inventory", 0)}
        return {"exists": False, "inventory": 0}

    # ---- writes ----
    def create_sales_order(self, customer_number: str, product_code: str, quantity: int) -> dict:
        payload = {"customerNumber": customer_number,
                   "salesOrderLines": [{"lineType": "Item", "lineObjectNumber": product_code, "quantity": quantity}]}
        d = self._post("salesOrders", payload)
        return {"created": True, "order_number": d.get("number"), "id": d.get("id")}

    def create_purchase_order(self, product_code: str, quantity: int, vendor_number: str = None) -> dict:
        vendor = self.resolve_vendor(vendor_number)
        payload = {"vendorNumber": vendor,
                   "purchaseOrderLines": [{"lineType": "Item", "lineObjectNumber": product_code, "quantity": quantity}]}
        d = self._post("purchaseOrders", payload)
        return {"created": True, "purchase_order_number": d.get("number"), "id": d.get("id"),
                "vendor": vendor, "vendor_name": d.get("vendorName", "")}
