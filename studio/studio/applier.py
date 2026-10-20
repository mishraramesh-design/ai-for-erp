"""Applies a validated change set to ERPNext, recording enough to undo every step (Charter D2, D5).

If any step fails, the steps already done are rolled back automatically, so a change set is all-or-nothing.
"""
from __future__ import annotations

from typing import Callable, Optional

from .compiler import (client_script_payload, custom_field_payload, doctype_payload, property_setter_payload,
                       property_value, server_script_payload, workflow_payload)
from .erp import ErpError

PM = "frappe.core.page.permission_manager.permission_manager"
RIGHTS = ("read", "write", "create", "delete", "submit", "cancel", "amend", "report", "export", "print", "email", "share")


class Applier:
    def __init__(self, erp, snap):
        self.erp = erp
        self.snap = snap

    # ------------------------------------------------------------ apply
    def apply(self, cs, on_progress: Optional[Callable[[int, int], None]] = None):
        steps: list = []
        total = len(cs.ops)
        for i, op in enumerate(cs.ops):
            try:
                step = self._do(i, op)
            except Exception as e:  # noqa: BLE001 - every failure must trigger rollback
                steps.append({"i": i, "op": op.op, "status": "failed", "error": str(e)})
                undone = self._undo_steps(steps[:-1])
                for s in steps[:-1]:
                    if s["status"] == "ok":
                        s["status"] = "rolled_back"
                err = f"Step {i + 1} ({op.op}) failed: {e}. Earlier steps were rolled back."
                if undone:
                    err += " Rollback problems: " + "; ".join(undone)
                self.snap.invalidate()
                return False, steps, err
            steps.append(step)
            if on_progress:
                on_progress(i + 1, total)
        self.snap.invalidate()
        return True, steps, ""

    def _do(self, i: int, op) -> dict:
        step = {"i": i, "op": op.op, "status": "ok", "created": [], "updated": [], "single": None, "perm": None, "note": ""}
        t = op.op
        if t == "add_custom_field":
            doc = self.erp.insert("Custom Field", custom_field_payload(op))
            step["created"].append(["Custom Field", doc.get("name") or f"{op.doctype}-{op.fieldname}"])
        elif t == "set_property":
            self._set_property(op, step)
        elif t in ("add_client_script", "add_server_validation"):
            dt = "Client Script" if t == "add_client_script" else "Server Script"
            payload = client_script_payload(op) if t == "add_client_script" else server_script_payload(op)
            existing = self.erp.get_doc(dt, payload["name"])
            if existing:
                before = {k: existing.get(k) for k in ("script", "enabled", "disabled", "doctype_event", "view") if k in existing}
                self.erp.update(dt, payload["name"], {k: v for k, v in payload.items() if k not in ("doctype", "name")})
                step["updated"].append([dt, payload["name"], before])
                step["note"] = "overwrote existing script"
            else:
                doc = self.erp.insert(dt, payload)
                step["created"].append([dt, doc.get("name") or payload["name"]])
        elif t == "set_setting":
            cur = self.erp.get_doc(op.doctype, op.doctype) or {}
            step["single"] = [op.doctype, op.field, cur.get(op.field)]
            self.erp.set_value(op.doctype, op.doctype, op.field, op.value)
        elif t == "create_role":
            if self.erp.get_doc("Role", op.role_name):
                step["status"], step["note"] = "skipped", "role already existed"
            else:
                doc = self.erp.insert("Role", {"doctype": "Role", "role_name": op.role_name, "desk_access": 1})
                step["created"].append(["Role", doc.get("name") or op.role_name])
                self._grant_to_self(op.role_name, step)
        elif t == "add_permission":
            self.erp.call_method(f"{PM}.add", {"parent": op.doctype, "role": op.role, "permlevel": op.permlevel})
            for r in RIGHTS:
                if getattr(op, r):
                    self.erp.call_method(f"{PM}.update", {"doctype": op.doctype, "role": op.role,
                                                          "permlevel": op.permlevel, "ptype": r, "value": 1})
            step["perm"] = [op.doctype, op.role, op.permlevel]
        elif t == "create_doctype":
            doc = self.erp.insert("DocType", doctype_payload(op))
            step["created"].append(["DocType", doc.get("name") or op.name])
        elif t == "create_workflow":
            for s in op.states:  # Workflow links need master records for every state and action
                if not self.erp.get_doc("Workflow State", s.state):
                    d = self.erp.insert("Workflow State", {"doctype": "Workflow State", "workflow_state_name": s.state})
                    step["created"].append(["Workflow State", d.get("name") or s.state])
            for a in sorted({x.action for x in op.transitions}):
                if not self.erp.get_doc("Workflow Action Master", a):
                    d = self.erp.insert("Workflow Action Master", {"doctype": "Workflow Action Master", "workflow_action_name": a})
                    step["created"].append(["Workflow Action Master", d.get("name") or a])
            doc = self.erp.insert("Workflow", workflow_payload(op))
            step["created"].append(["Workflow", doc.get("name") or op.name])
        else:  # pragma: no cover - the discriminated union makes this unreachable
            raise ValueError(f"unknown operation {t}")
        return step

    def _grant_to_self(self, role: str, step: dict) -> None:
        """Give a newly created role to the service user so the Studio can read and test what it builds.
        Undo restores the user's previous roles before the role is deleted."""
        try:
            me = self.erp.call_method("frappe.auth.get_logged_user", {})
            user = self.erp.get_doc("User", me) if me else None
            if not user:
                return
            before = [{"role": r["role"]} for r in user.get("roles", [])]
            if role in {r["role"] for r in before}:
                return
            self.erp.update("User", me, {"roles": before + [{"role": role}]})
            step["updated"].append(["User", me, {"roles": before}])
        except Exception as e:  # noqa: BLE001 - not fatal: the change itself succeeded
            step["note"] = f"role created, but could not be granted to the Studio user ({e})"

    def _set_property(self, op, step: dict) -> None:
        filters = [["doc_type", "=", op.doctype], ["property", "=", op.property]]
        filters.append(["field_name", "=", op.fieldname] if op.fieldname else ["doctype_or_field", "=", "DocType"])
        found = self.erp.list_docs("Property Setter", fields=["name", "value"], filters=filters, limit=1)
        value = property_value(op)
        if found:
            name = found[0]["name"]
            self.erp.update("Property Setter", name, {"value": value})
            step["updated"].append(["Property Setter", name, {"value": found[0].get("value")}])
            step["note"] = "updated existing property"
        else:
            doc = self.erp.insert("Property Setter", property_setter_payload(op))
            step["created"].append(["Property Setter", doc.get("name")])

    # ------------------------------------------------------------ undo
    def undo(self, cs) -> list:
        errors = self._undo_steps([s for s in cs.steps if s.get("status") == "ok"])
        self.snap.invalidate()
        return errors

    def _undo_steps(self, steps: list) -> list:
        errors: list = []
        for s in reversed(steps):
            if s.get("status") not in ("ok",):
                continue
            try:
                if s.get("perm"):
                    dt, role, lvl = s["perm"]
                    self.erp.call_method(f"{PM}.remove", {"doctype": dt, "role": role, "permlevel": lvl, "if_owner": 0})
                if s.get("single"):
                    dt, field, before = s["single"]
                    self.erp.set_value(dt, dt, field, before if before is not None else "")
                for dt, name, before in reversed(s.get("updated") or []):
                    self.erp.update(dt, name, before)
                for dt, name in reversed(s.get("created") or []):
                    self.erp.delete(dt, name)
            except ErpError as e:
                if e.status == 404:
                    continue
                errors.append(f"step {s.get('i', 0) + 1}: {e.detail}")
            except Exception as e:  # noqa: BLE001
                errors.append(f"step {s.get('i', 0) + 1}: {e}")
        return errors
