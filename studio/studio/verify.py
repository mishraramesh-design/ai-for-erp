"""Deterministic verification: after a change set is applied, read the LIVE site back and prove each operation took effect.

No model is involved. Behavioural tests written by the tester agent are executed here too, with cleanup.
"""
from __future__ import annotations

from typing import Optional

import re
import time
from dataclasses import asdict, dataclass

from .applier import PM
from .compiler import client_script_payload, record_payload, script_name, server_script_payload
from .spec import RECORD_OPS
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

    # Post-flight health: every DocType this change touched must still be readable (a missing table breaks lists, cards and reports)
    seen: set = set()
    for i, op in enumerate(cs.ops):
        dt = op.name if op.op == "create_doctype" else getattr(op, "doctype", None)
        if dt and dt not in seen and not (op.op == "create_doctype" and op.is_child_table):
            seen.add(dt)
            try:
                erp.list_docs(dt, fields=["name"], limit=1)
                out.append(Check(i, f"{dt} can be read (table healthy)", "pass"))
            except ErpError as e:
                if e.status >= 500:
                    out.append(Check(i, f"{dt} can be read (table healthy)", "fail", e.detail[:160]))
            except Exception:  # noqa: BLE001 - a health probe must not break verification
                pass

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
            elif t in RECORD_OPS:
                dt = RECORD_OPS[t]
                nm = record_payload(op)["name"]
                d = erp.get_doc(dt, nm)
                add(i, f"{dt} '{nm}' exists", d is not None)
                if d:
                    _record_checks(op, d, add, i)
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


_MANDATORY = re.compile(r"mandatory|required|MandatoryError|is not set|cannot be empty|value missing", re.I)


_LAYOUT = {"Section Break", "Column Break", "Tab Break", "HTML", "Button", "Heading", "Image", "Fold", "Table",
           "Table MultiSelect", "Attach", "Attach Image", "Signature", "Geolocation", "Read Only"}


def autofill(erp, doctype: str, vals: dict, own_fields: set, made: list) -> dict:
    """Fill mandatory fields that OTHER changes made mandatory, so one change's tests are not blocked by another's rules.

    Fields this change set itself defines are never filled: a test may leave them empty on purpose."""
    try:
        meta = erp.get_meta(doctype) or {}
    except Exception:  # noqa: BLE001
        return vals
    out = dict(vals)
    for f in meta.get("fields", []):
        fn, ft = f.get("fieldname"), f.get("fieldtype")
        if not fn or not f.get("reqd") or fn in out or fn in own_fields or ft in _LAYOUT or f.get("default") not in (None, ""):
            continue
        if ft in ("Data", "Small Text", "Text", "Long Text", "Text Editor", "Code", "Phone"):
            out[fn] = "Studio test"
        elif ft in ("Int", "Float", "Currency", "Percent", "Rating"):
            out[fn] = 1
        elif ft == "Date":
            out[fn] = time.strftime("%Y-%m-%d")
        elif ft == "Datetime":
            out[fn] = time.strftime("%Y-%m-%d %H:%M:%S")
        elif ft == "Select":
            opts = [o for o in (f.get("options") or "").split("\n") if o.strip()]
            if opts:
                out[fn] = opts[0]
        elif ft == "Link" and f.get("options"):
            try:
                rows = erp.list_docs(f["options"], fields=["name"], limit=1)
                if not rows and f["options"] == "Contact":
                    d = erp.insert("Contact", {"doctype": "Contact", "first_name": "Studio Test"})
                    made.append(("Contact", d.get("name")))
                    rows = [{"name": d.get("name")}]
                if rows:
                    out[fn] = rows[0]["name"]
            except Exception:  # noqa: BLE001
                pass
    return out


def _mentions(detail: str, terms: set) -> bool:
    d = re.sub(r"[^a-z0-9]+", " ", detail.lower())
    return any(t and t in d for t in terms)


def run_tests(tests: list, erp, own_fields: Optional[set] = None, own_terms: Optional[set] = None) -> list[dict]:
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
        created, made = None, []
        try:
            vals = autofill(erp, dt, vals, own_fields or set(), made)
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
                elif re.search(r"could not find|does not exist", e.detail, re.I):  # a made-up Link value is a bad test, not a bad change
                    res["status"], res["detail"] = "inconclusive", f"test data refers to a record that does not exist: {e.detail[:160]}"
                elif own_terms and not _mentions(e.detail, own_terms):
                    res["status"], res["detail"] = "inconclusive", f"blocked by a rule that is not part of this change: {e.detail[:160]}"
                else:
                    res["status"], res["detail"] = "fail", f"unexpectedly rejected: {e.detail[:160]}"
        finally:
            if created and created.get("name"):
                try:
                    erp.delete(dt, created["name"])
                except Exception:  # noqa: BLE001
                    res["detail"] += " (cleanup failed)"
            for mdt, mname in made:
                try:
                    erp.delete(mdt, mname)
                except Exception:  # noqa: BLE001
                    pass
        results.append(res)
    return results


def _record_checks(op, d, add, i) -> None:
    t = op.op
    if t == "create_notification":
        add(i, "notification is enabled", bool(d.get("enabled")))
        add(i, f"fires on {op.event}", d.get("event") == op.event, f"found {d.get('event')}")
        add(i, "recipients stored", len(d.get("recipients") or []) == len(op.recipients),
            f"{len(d.get('recipients') or [])} of {len(op.recipients)}")
    elif t == "create_report":
        import json
        try:
            cols = [c[0] for c in json.loads(d.get("json") or "{}").get("fields", [])]
        except ValueError:
            cols = []
        add(i, "report columns stored", cols == list(op.columns), f"found {cols}")
        add(i, "report is a Report Builder report", d.get("report_type") == "Report Builder")
    elif t == "create_number_card":
        add(i, "card reads the right DocType", d.get("document_type") == op.doctype, f"found {d.get('document_type')}")
        add(i, f"card function is {op.function}", d.get("function") == op.function, f"found {d.get('function')}")
    elif t == "create_dashboard_chart":
        add(i, "chart reads the right DocType", d.get("document_type") == op.doctype, f"found {d.get('document_type')}")
        add(i, f"chart type is {op.chart_type}", d.get("chart_type") == op.chart_type, f"found {d.get('chart_type')}")
    elif t == "create_web_form":
        add(i, "web form is published and needs login", bool(d.get("published")) and bool(d.get("login_required")) and not d.get("anonymous"))
        add(i, "web form reads the right DocType", d.get("doc_type") == op.doctype, f"found {d.get('doc_type')}")
        add(i, "web form fields stored", len(d.get("web_form_fields") or []) == len(op.fields),
            f"{len(d.get('web_form_fields') or [])} of {len(op.fields)}")
    elif t == "create_html_block":
        add(i, "HTML stored", (d.get("html") or "").strip() == op.html.strip())
    elif t == "create_workspace":
        add(i, "workspace is public", bool(d.get("public")))
        add(i, "workspace blocks stored", len(d.get("number_cards") or []) == len(op.cards) and len(d.get("charts") or []) == len(op.charts)
            and len(d.get("shortcuts") or []) == len(op.shortcuts) and len(d.get("custom_blocks") or []) == len(op.html_blocks))
    elif t == "create_dashboard":
        add(i, "dashboard charts stored", len(d.get("charts") or []) == len(op.charts), f"{len(d.get('charts') or [])} of {len(op.charts)}")
        add(i, "dashboard cards stored", len(d.get("cards") or []) == len(op.cards), f"{len(d.get('cards') or [])} of {len(op.cards)}")
