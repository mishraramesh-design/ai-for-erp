"""Thin client for the ERPNext REST API, behind a Protocol so tests and later adapters can swap it (D7)."""
from __future__ import annotations

import json
import re
from typing import Any, Optional, Protocol
from urllib.parse import quote

import httpx


class ErpError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(f"ERPNext {status}: {detail}")
        self.status = status
        self.detail = detail


class ErpClient(Protocol):
    def list_docs(self, doctype: str, fields: Optional[list] = None, filters: Optional[list] = None,
                  limit: int = 0) -> list[dict]: ...

    def get_doc(self, doctype: str, name: str) -> Optional[dict]: ...

    def insert(self, doctype: str, data: dict) -> dict: ...

    def update(self, doctype: str, name: str, data: dict) -> dict: ...

    def delete(self, doctype: str, name: str) -> None: ...

    def set_value(self, doctype: str, name: str, field: str, value: Any) -> None: ...

    def call_method(self, method: str, data: dict) -> Any: ...

    def get_meta(self, doctype: str) -> Optional[dict]: ...


def _detail(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except Exception:
        return resp.text[:500]
    msgs: list[str] = []
    sm = body.get("_server_messages") if isinstance(body, dict) else None
    if sm:
        try:
            for m in json.loads(sm):
                try:
                    msgs.append(str(json.loads(m).get("message", m)))
                except Exception:
                    msgs.append(str(m))
        except Exception:
            msgs.append(str(sm))
    if not msgs and isinstance(body, dict):
        for key in ("exception", "message", "exc_type"):
            if body.get(key):
                msgs.append(str(body[key]))
                break
    text = re.sub(r"<[^>]+>", "", " | ".join(msgs))
    return (text or resp.text)[:800]


class HttpErp:
    def __init__(self, base_url: str, api_key: str, api_secret: str, host_header: Optional[str] = None,
                 timeout: float = 90.0, client: Optional[httpx.Client] = None):
        headers = {"Authorization": f"token {api_key}:{api_secret}", "Accept": "application/json"}
        if host_header:
            headers["Host"] = host_header
        self._c = client or httpx.Client(base_url=base_url.rstrip("/"), headers=headers, timeout=timeout)

    # -- helpers
    def _req(self, method: str, url: str, **kw) -> dict:
        r = self._c.request(method, url, **kw)
        if r.status_code >= 400:
            raise ErpError(r.status_code, _detail(r))
        try:
            return r.json()
        except Exception:
            return {}

    @staticmethod
    def _path(doctype: str, name: Optional[str] = None) -> str:
        p = f"/api/resource/{quote(doctype, safe='')}"
        return p + (f"/{quote(name, safe='')}" if name is not None else "")

    # -- protocol
    def list_docs(self, doctype, fields=None, filters=None, limit=0):
        params: dict = {"limit_page_length": limit}
        if fields:
            params["fields"] = json.dumps(fields)
        if filters:
            params["filters"] = json.dumps(filters)
        return self._req("GET", self._path(doctype), params=params).get("data", [])

    def get_doc(self, doctype, name):
        try:
            return self._req("GET", self._path(doctype, name)).get("data")
        except ErpError as e:
            if e.status == 404:
                return None
            raise

    def insert(self, doctype, data):
        return self._req("POST", self._path(doctype), json=data).get("data", {})

    def update(self, doctype, name, data):
        return self._req("PUT", self._path(doctype, name), json=data).get("data", {})

    def delete(self, doctype, name):
        self._req("DELETE", self._path(doctype, name))

    def set_value(self, doctype, name, field, value):
        self._req("POST", "/api/method/frappe.client.set_value",
                  json={"doctype": doctype, "name": name, "fieldname": field, "value": value})

    def call_method(self, method, data):
        return self._req("POST", f"/api/method/{method}", json=data).get("message")

    def get_meta(self, doctype):
        try:
            body = self._req("GET", "/api/method/frappe.desk.form.load.getdoctype", params={"doctype": doctype})
        except ErpError as e:
            if e.status == 404 or "not found" in e.detail.lower():
                return None
            raise
        docs = body.get("docs") or (body.get("message") or {}).get("docs") or []
        main = next((d for d in docs if d.get("name") == doctype), docs[0] if docs else None)
        if not main:
            return None
        return {
            "name": main.get("name", doctype),
            "module": main.get("module"),
            "issingle": bool(main.get("issingle")),
            "istable": bool(main.get("istable")),
            "custom": bool(main.get("custom")),
            "is_submittable": bool(main.get("is_submittable")),
            "fields": [
                {k: f.get(k) for k in ("fieldname", "label", "fieldtype", "options", "reqd", "hidden", "read_only", "in_list_view", "in_standard_filter", "allow_on_submit", "bold", "print_hide", "unique", "default", "description", "depends_on", "mandatory_depends_on", "read_only_depends_on")}
                for f in main.get("fields", [])
            ],
        }
