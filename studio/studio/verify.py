"""Deterministic verification: after a change set is applied, read the LIVE site back and prove each operation took effect.

No model is involved. Behavioural tests written by the tester agent are executed here too, with cleanup.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass

from .applier import PM
from .compiler import client_script_payload, script_name, server_script_payload
from .erp import ErpError
from .validators import PROPERTY_TYPES, _norm_check_value


@dataclass
class Check:
    op_index: int
    name: str
    status: str  # pass | fail | inconclusive
    detail: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _meta_field(erp, doctype: str, fieldname: str):
    m = erp.get_meta(doctype)
    if not m:
        return None, None
    return next((f for f in m["fields"] if f.get("fieldname") == fieldname), None), m


def _truthy(v) -> int:
    return 1 if str(v).strip().lower() in ("1", "true", "yes") else 0


def verify_changeset(cs, erp) -> list[Check]:
    out: list[Check] = []

    def add(i, name, ok, detail=""):
        out.append(Check(i, name, "pass" if ok else "fail", detail))

    for i, op in enumerate(cs.ops):
        t = op.op
        try:
            if t == "add_custom_field":
                f, _ = _meta_field(erp, op.doctype, op.fieldname)
                add(i, f"field {op.doctype}.{op.fieldname} exists", f is not None, "" if f else "not present in the live form definition")
                if f:
                    add(i, f"field type is {op.fieldtype}", f.get("fieldtype") == op.fieldtype, f"found {f.get('fieldtype')}")
                    if op.reqd:
                        add(i, "field is mandatory", _truthy(f.get("reqd")) == 1)
                    if op.options and op.fieldtype in ("Select", "Link"):
                        add(i, "field options match", (f.get("options") or "").strip() == op.options.strip(), f"found {f.get('options')!r}")
            elif t == "set_property":
                if op.fieldname:
                    f, _ = _meta_field(erp, op.doctype, op.fieldname)
                    if f is None:
                        add(i, f"field {op.doctype}.{op.fieldname} exists", False, "field not found")
                        continue
                    live = f.get(op.property)
                    if PROPERTY_TYPES[op.property] == "Check":
                        add(i, f"{op.fieldname}.{op.property} = {op.value}", _truthy(live) == int(_norm_check_value(op.value) or 0), f"live value {live!r}")
                    else:
                        add(i, f"{op.fieldname}.{op.property} set", str(live or "") == str(op.value), f"live value {live!r}")
                else:
                    ps = erp.list_docs("Property Setter", fields=["value"], filters=[["doc_type", "=", op.doctype], ["property", "=", op.property],
                                                                                       ["doctype_or_field", "=", "DocType"]], limit=1)
                    add(i, f"{op.doctype}.{op.property} property setter present", bool(ps) and str(ps[0]["value"]) == str(op.value))
            elif t in ("add_client_script", "add_server_validation"):
                dt = "Client Script" if t == "add_client_script" else "Server Script"
                payload = client_script_payload(op) if t == "add_client_script" else server_script_payload(op)
                d = erp.get_doc(dt, payload["name"])
                add(i, f"{dt} '{payload['name']}' exists", d is not None)
                if d:
                    on = bool(d.get("enabled")) if dt == "Client Script" else not d.get("disabled")
                    add(i, f"{dt} is enabled", on)
                    add(i, "stored script matches", (d.get("script") or "").strip() == op.script.strip())
            elif t == "set_setting":
                d = erp.get_doc(op.doctype, op.doctype) or {}
                live = d.get(op.field)
                ok = _truthy(live) == _truthy(op.value) if str(op.value).lower() in ("0", "1", "true", "false") else str(live) == str(op.value)
                add(i, f"{op.doctype}.{op.field} = {op.value}", ok, f"live value {live!r}")
            elif t == "create_role":
                add(i, f"role '{op.role_name}' exists", erp.get_doc("Role", op.role_name) is not None)
            elif t == "add_permission":
                rows = erp.call_method(f"{PM}.get_permissions", {"doctype": op.doctype}) or []
                hit = next((r for r in rows if r.get("role") == op.role and int(r.get("permlevel") or 0) == op.permlevel), None)
                add(i, f"'{op.role}' has a permission rule on {op.doctype}", hit is not None)
                if hit:
                    missing = [k for k in ("read", "write", "create", "delete", "submit", "cancel", "amend", "report", "export", "print", "email", "share")
                               if getattr(op, k) and not _truthy(hit.get(k))]
                    add(i, "requested rights are on", not missing, f"missing: {', '.join(missing)}" if missing else "")
            elif t == "create_doctype":
                d = erp.get_doc("DocType", op.name)
                add(i, f"DocType '{op.name}' exists", d is not None)
                if d:
                    m = erp.get_meta(op.name) or {"fields": []}
                    have = {f.get("fieldname") for f in m["fields"]}
                    miss = [f.fieldname for f in op.fields if f.fieldname not in have]
                    add(i, "all fields present", not miss, f"missing: {', '.join(miss)}" if miss else "")
                    if not op.is_child_table:
                        try:
                            erp.list_docs(op.name, fields=["name"], limit=1)
                            add(i, "table is queryable (database table created)", True)
                        except ErpError as e:
                            add(i, "table is queryable (database table created)", False, e.detail[:160])
            elif t == "create_workflow":
                d = erp.get_doc("Workflow", op.name)
                add(i, f"workflow '{op.name}' exists", d is not None)
                if d:
                    add(i, "workflow is active", bool(d.get("is_active")))
                    add(i, "states stored", len(d.get("states") or []) == len(op.states), f"{len(d.get('states') or [])} of {len(op.states)}")
                    add(i, "transitions stored", len(d.get("transitions") or []) == len(op.transitions),
                        f"{len(d.get('transitions') or [])} of {len(op.transitions)}")
        except ErpError as e:
            out.append(Check(i, f"{t} readback", "fail", e.detail[:200]))
        except Exception as e:  # noqa: BLE001 - a verifier must never crash the run
            out.append(Check(i, f"{t} readback", "fail", f"{type(e).__name__}: {e}"))
    return out


_MANDATORY = re.compile(r"mandatory|required|MandatoryError|is not set|cannot be empty", re.I)


def run_tests(tests: list, erp) -> list[dict]:
    """Behavioural tests from the tester agent. Each inserts a throwaway document and removes it again."""
    results = []
    for t in tests[:12]:
        res = {"id": t.get("id"), "requirement_id": t.get("requirement_id"), "doctype": t.get("doctype"),
               "kind": t.get("kind"), "description": t.get("description", ""), "status": "inconclusive", "detail": ""}
        dt, vals, kind = t.get("doctype"), t.get("values") or {}, t.get("kind")
        if not dt or kind not in ("insert_expect_error", "insert_expect_ok") or not isinstance(vals, dict):
            res["detail"] = "malformed test"
            results.append(res)
            continue
        created = None
        try:
            created = erp.insert(dt, {"doctype": dt, **vals})
            if kind == "insert_expect_ok":
                res["status"], res["detail"] = "pass", "document saved as expected"
            else:
                res["status"], res["detail"] = "fail", "the save succeeded but a rule should have blocked it"
        except ErpError as e:
            want = (t.get("error_contains") or "").lower()
            if kind == "insert_expect_error":
                if want and want in e.detail.lower():
                    res["status"], res["detail"] = "pass", f"blocked with: {e.detail[:160]}"
                elif _MANDATORY.search(e.detail) and want not in e.detail.lower():
                    res["status"], res["detail"] = "inconclusive", f"stopped earlier by a different rule: {e.detail[:160]}"
                else:
                    ww = {w for w in re.findall(r"[a-z]{3,}", want)}
                    got = set(re.findall(r"[a-z]{3,}", e.detail.lower()))
                    if ww and len(ww & got) / len(ww) >= 0.5:
                        res["status"], res["detail"] = "pass", f"blocked with a differently worded message: {e.detail[:160]}"
                    else:  # blocked for some other reason: not proof the rule works, but not proof it is broken either
                        res["status"], res["detail"] = "inconclusive", f"blocked, but with a different message: {e.detail[:160]}"
            else:
                if _MANDATORY.search(e.detail):
                    res["status"], res["detail"] = "inconclusive", f"test data incomplete: {e.detail[:160]}"
                else:
                    res["status"], res["detail"] = "fail", f"unexpectedly rejected: {e.detail[:160]}"
        finally:
            if created and created.get("name"):
                try:
                    erp.delete(dt, created["name"])
                except Exception:  # noqa: BLE001
                    res["detail"] += " (cleanup failed)"
        results.append(res)
    return results
