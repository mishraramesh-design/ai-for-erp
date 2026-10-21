"""Cached read-only view of the live ERPNext schema: the deterministic 'knowledge' the agent works from (R8)."""
from __future__ import annotations

import re
import threading
import time
from typing import Optional


def _norm(text: str) -> str:
    t = re.sub(r"\bfield\b", "", (text or "").lower())
    return re.sub(r"[^a-z0-9]+", " ", t).strip()


class Snapshot:
    def __init__(self, erp, ttl: float = 300.0):
        self.erp = erp
        self.ttl = ttl
        self._lock = threading.RLock()
        self._doctypes: Optional[dict] = None
        self._doctypes_at = 0.0
        self._metas: dict = {}
        self._tables: dict = {}
        self._roles: Optional[set] = None
        self._roles_at = 0.0
        self._modules: Optional[set] = None
        self._modules_at = 0.0

    def invalidate(self) -> None:
        with self._lock:
            self._doctypes = None
            self._metas = {}
            self._roles = None
            self._modules = None

    def _stale(self, at: float) -> bool:
        return (time.time() - at) > self.ttl

    def doctypes(self) -> dict:
        with self._lock:
            if self._doctypes is None or self._stale(self._doctypes_at):
                rows = self.erp.list_docs("DocType", fields=["name", "module", "istable", "issingle", "custom"], limit=0)
                self._doctypes = {r["name"]: r for r in rows}
                self._doctypes_at = time.time()
            return self._doctypes

    def doctype_names(self) -> list[str]:
        return sorted(self.doctypes())

    def has_doctype(self, name: str) -> bool:
        return name in self.doctypes()

    def meta(self, name: str) -> Optional[dict]:
        with self._lock:
            if name not in self.doctypes():
                return None
            hit = self._metas.get(name)
            if hit and not self._stale(hit[0]):
                return hit[1]
            meta = self.erp.get_meta(name)
            self._metas[name] = (time.time(), meta)
            return meta

    def fieldnames(self, doctype: str) -> set:
        m = self.meta(doctype)
        return {f["fieldname"] for f in (m or {}).get("fields", []) if f.get("fieldname")}

    def find_field(self, doctype: str, text: str) -> Optional[dict]:
        """Resolve a user's wording ('customer', 'Posting Date', 'grand total') to a real field."""
        m = self.meta(doctype)
        if not m or not text:
            return None
        want = _norm(text)
        if not want:
            return None
        fields = [f for f in m["fields"] if f.get("fieldname")]
        for f in fields:
            if _norm(f["fieldname"]) == want or _norm(f.get("label") or "") == want:
                return f
        for f in fields:
            lab = _norm(f.get("label") or "")
            if lab and (lab.startswith(want) or want in lab.split()):
                return f
        return None

    def roles(self) -> set:
        with self._lock:
            if self._roles is None or self._stale(self._roles_at):
                self._roles = {r["name"] for r in self.erp.list_docs("Role", fields=["name"], limit=0)}
                self._roles_at = time.time()
            return self._roles

    def modules(self) -> set:
        with self._lock:
            if self._modules is None or self._stale(self._modules_at):
                self._modules = {r["name"] for r in self.erp.list_docs("Module Def", fields=["name"], limit=0)}
                self._modules_at = time.time()
            return self._modules

    def active_workflow(self, doctype: str) -> Optional[str]:
        rows = self.erp.list_docs("Workflow", fields=["name"],
                                  filters=[["document_type", "=", doctype], ["is_active", "=", 1]], limit=1)
        return rows[0]["name"] if rows else None

    def custom_doctypes(self) -> list[str]:
        return sorted(n for n, r in self.doctypes().items() if r.get("custom"))

    def table_ok(self, doctype: str) -> bool:
        """False when the DocType record exists but reading its rows fails on the server (e.g. its table is missing)."""
        with self._lock:
            hit = self._tables.get(doctype)
            if hit and not self._stale(hit[0]):
                return hit[1]
        ok = True
        try:
            self.erp.list_docs(doctype, fields=["name"], limit=1)
        except Exception as e:  # noqa: BLE001 - only a server error means a broken table; permissions or 404 do not
            ok = getattr(e, "status", 0) < 500
        with self._lock:
            self._tables[doctype] = (time.time(), ok)
        return ok

    def site_facts(self, doctype: str) -> list[str]:
        """What is ALREADY built on a DocType (custom fields, workflow, scripts, notifications, reports, web forms)."""
        out: list[str] = []

        def rows(dt, fields, filters):
            try:
                return self.erp.list_docs(dt, fields=fields, filters=filters, limit=40)
            except Exception:  # noqa: BLE001 - facts are a hint, never a reason to fail
                return []
        cf = [r["fieldname"] for r in rows("Custom Field", ["fieldname"], [["dt", "=", doctype]]) if r.get("fieldname")]
        if cf:
            out.append("custom fields: " + ", ".join(cf))
        for label, dt, field, fl in (("workflow", "Workflow", "name", "document_type"), ("server scripts", "Server Script", "name", "reference_doctype"),
                                     ("notifications", "Notification", "name", "document_type"), ("reports", "Report", "name", "ref_doctype"),
                                     ("web forms", "Web Form", "name", "doc_type")):
            names = [r["name"] for r in rows(dt, ["name"], [[fl, "=", doctype]])]
            if names:
                out.append(f"{label}: " + ", ".join(names))
        return out

    def doc_exists(self, doctype: str, name: str) -> bool:
        return self.erp.get_doc(doctype, name) is not None
