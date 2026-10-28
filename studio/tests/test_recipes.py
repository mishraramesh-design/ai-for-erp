import json

from studio.applier import Applier
from studio.exporter import build
from studio.snapshot import Snapshot
from studio.spec import ChangeSet
from studio.validators import has_errors, validate
from studio.verify import verify_changeset
from tests.fakes import FakeErp
from tests.test_exporter import make

OPS = [
    {"op": "add_custom_field", "doctype": "Supplier", "fieldname": "custom_risk", "label": "Risk", "fieldtype": "Select",
     "options": "Low\nHigh"},
    {"op": "create_notification", "name": "High risk supplier", "doctype": "Supplier", "event": "Save",
     "subject": "Risk {{ doc.supplier_name }}", "message": "<p>{{ doc.supplier_name }} is high risk</p>",
     "condition": "doc.custom_risk == 'High'", "recipients": [{"role": "System Manager"}]},
    {"op": "create_report", "name": "Supplier risk list", "doctype": "Supplier",
     "columns": ["name", "supplier_name", "custom_risk"], "filters": [{"field": "custom_risk", "value": "High"}]},
    {"op": "create_number_card", "label": "High risk suppliers", "doctype": "Supplier",
     "filters": [{"field": "custom_risk", "value": "High"}]},
    {"op": "create_dashboard_chart", "name": "Suppliers by risk", "doctype": "Supplier", "group_by_field": "custom_risk"},
    {"op": "create_html_block", "name": "demo-pilot banner", "html": "<div class='dp'>demo-pilot</div>", "style": ".dp{background:#FFE600;color:#000}"},
    {"op": "create_dashboard", "name": "SRM", "charts": ["Suppliers by risk"], "cards": ["High risk suppliers"]},
    {"op": "create_workspace", "title": "SRM Home", "html_blocks": ["demo-pilot banner"], "cards": ["High risk suppliers"],
     "charts": ["Suppliers by risk"], "shortcuts": [{"label": "Suppliers", "doctype": "Supplier"}]},
]


def codes(ops):
    erp = FakeErp()
    issues = validate(ChangeSet(ops=ops), Snapshot(erp, ttl=0))
    return [i.code for i in issues if i.level == "error"]


def test_valid_recipe_set_has_no_errors():
    assert codes(OPS) == []


def test_unknown_field_role_and_missing_pieces_are_errors():
    bad = [
        {"op": "create_notification", "name": "Bad one", "doctype": "Supplier", "event": "Save", "subject": "s",
         "message": "{{ doc.nope }}", "condition": "doc.zzz == 1", "recipients": [{"role": "No Such Role"}]},
        {"op": "create_notification", "name": "Bad two", "doctype": "Supplier", "event": "Days Before", "subject": "s",
         "message": "m", "recipients": []},
        {"op": "create_report", "name": "Bad report", "doctype": "Supplier", "columns": ["missing_col"]},
        {"op": "create_number_card", "label": "Bad card", "doctype": "Supplier", "function": "Sum", "field": "supplier_name"},
        {"op": "create_dashboard", "name": "Bad dash", "charts": ["ghost"], "cards": []},
    ]
    got = codes(bad)
    for want in ("field_missing", "role_missing", "no_recipients", "date_field", "field_numeric", "chart_missing"):
        assert want in got, (want, got)


def test_apply_verify_undo_and_export(tmp_path):
    ledger, reqs, cs = make(tmp_path, OPS)
    erp = FakeErp()
    snap = Snapshot(erp, ttl=0)
    cs2 = ChangeSet(ops=OPS)
    ok, steps, err = Applier(erp, snap).apply(cs2)
    assert ok, err
    cs2.steps = steps
    assert all(c.status == "pass" for c in verify_changeset(cs2, erp)), [c for c in verify_changeset(cs2, erp) if c.status != "pass"]
    assert Applier(erp, snap).undo(cs2) == []
    assert erp.get_doc("Notification", "High risk supplier") is None
    out = build(ledger, reqs)
    for fx in ("custom_html_block", "workspace", "notification", "report", "number_card", "dashboard_chart", "dashboard"):
        assert any(p.endswith(f"fixtures/{fx}.json") for p in out.files), (fx, sorted(out.files))
    rep = json.loads(next(v for p, v in out.files.items() if p.endswith("fixtures/report.json")))[0]
    assert rep["report_type"] == "Report Builder" and json.loads(rep["json"])["fields"][0] == ["name", "Supplier"]
    hooks = next(v for p, v in out.files.items() if p.endswith("hooks.py"))
    assert hooks.index("'Report'") < hooks.index("'Notification'") < hooks.index("'Dashboard'")


def test_html_block_rejects_scripts():
    got = codes([{"op": "create_html_block", "name": "bad block", "html": "<img src=x onerror=alert(1)>"}])
    assert "html_script" in got


def test_inline_style_tag_in_html_block_is_rejected():
    got = codes([{"op": "create_html_block", "name": "styled", "html": "<style>.a{color:red}</style><div class='a'>x</div>"}])
    assert "html_inline_style" in got


def test_workflow_state_field_is_known_after_a_planned_workflow():
    ops = [{"op": "create_workflow", "name": "Supplier flow", "doctype": "Supplier",
            "states": [{"state": "Draft", "doc_status": 0, "allow_edit": "System Manager"},
                       {"state": "Approved", "doc_status": 0, "allow_edit": "System Manager"}],
            "transitions": [{"state": "Draft", "action": "Approve", "next_state": "Approved", "allowed": "System Manager"}]},
           {"op": "create_report", "name": "By state", "doctype": "Supplier", "columns": ["name", "workflow_state"]}]
    assert "field_missing" not in codes(ops)


def test_normalize_ops_repairs_common_model_slips():
    from studio.spec import LlmProposal, normalize_ops
    raw = [{"create_role": {"role_name": "X Role"}},
           {"type": "Create-Notification", "name": "N one", "doctype": "Supplier", "event": "Value Change", "subject": "s",
            "message": "m", "value_changed": ["disabled"], "recipients": [{"role": "System Manager", "field": None}]},
           {"op": "create_notification", "name": "N two", "doctype": "Supplier", "event": "Days Before", "subject": "s",
            "message": "m", "date_field": "creation", "days": "3", "recipients": [{"role": "System Manager"}]}]
    prop = LlmProposal.model_validate({"ops": normalize_ops(raw)})
    assert [o.op for o in prop.ops] == ["create_role", "create_notification", "create_notification"]
    assert prop.ops[1].value_changed == "disabled" and prop.ops[2].days == 3


def test_administrator_role_cannot_be_granted():
    ops = [{"op": "create_doctype", "name": "Zed Doc", "fields": [{"fieldname": "title", "label": "Title", "fieldtype": "Data"}],
            "permissions": [{"role": "Administrator", "read": True}]}]
    assert "perm_system_role" in codes(ops)


def test_date_defaults_are_normalised_for_frappe():
    from studio.compiler import norm_default
    assert norm_default("Date", "today") == "Today" and norm_default("Date", "Now") == "Today"
    assert norm_default("Datetime", "today") == "Now" and norm_default("Date", "2026-10-21") == "2026-10-21"
    assert norm_default("Date", "next week") is None and norm_default("Data", "today") == "today"


def test_select_default_must_be_one_of_the_options():
    from tests.fakes import FakeErp
    erp = FakeErp()
    snap = Snapshot(erp, ttl=0)
    orig = snap.meta
    snap.meta = lambda dt: ({**(orig(dt) or {}), "fields": (orig(dt) or {}).get("fields", []) + [
        {"fieldname": "naming_series", "fieldtype": "Select", "options": "SUP-.#####", "default": ""}]} if dt == "Supplier" else orig(dt))
    bad = [{"op": "set_property", "doctype": "Supplier", "fieldname": "naming_series", "property": "default", "value": "SUP-.YYYY.-"}]
    assert "select_default" in [i.code for i in validate(ChangeSet(ops=bad), snap) if i.level == "error"]
    good = [{"op": "set_property", "doctype": "Supplier", "fieldname": "naming_series", "property": "options", "value": "SUP-.YYYY.-\nSUP-.#####"},
            {"op": "set_property", "doctype": "Supplier", "fieldname": "naming_series", "property": "default", "value": "SUP-.YYYY.-"}]
    assert "select_default" not in [i.code for i in validate(ChangeSet(ops=good), snap) if i.level == "error"]


def test_web_form_validation_and_name():
    from studio.compiler import web_form_name, web_form_payload
    from studio.spec import OpAdapter
    base = {"op": "create_web_form", "title": "Supplier Risk Form", "route": "supplier-risk", "doctype": "Supplier",
            "fields": [{"fieldname": "supplier_name", "fieldtype": "Data", "reqd": True}], "list_columns": ["supplier_name"]}
    assert codes([base]) == []
    assert web_form_name("Supplier Risk Form") == "supplier-risk-form"
    p = web_form_payload(OpAdapter.validate_python(base))
    assert p["login_required"] == 1 and p["anonymous"] == 0 and p["apply_document_permissions"] == 1
    assert "web_route" in codes([{**base, "route": "Bad Route"}])
    assert "web_title" in codes([{**base, "title": "Bad/Title!"}])
    assert "field_missing" in codes([{**base, "fields": [{"fieldname": "nope", "fieldtype": "Data"}]}])
    assert "web_column" in codes([{**base, "list_columns": ["supplier_group"]}])


def test_broken_table_blocks_plans_unless_rebuilt():
    from studio.erp import ErpError
    erp = FakeErp()
    real = erp.list_docs

    def broken(doctype, fields=None, filters=None, limit=0):
        if doctype == "Supplier":
            raise ErpError(500, "Table 'tabSupplier' doesn't exist")
        return real(doctype, fields, filters, limit)
    erp.list_docs = broken
    snap = Snapshot(erp, ttl=0)
    ops = [{"op": "add_custom_field", "doctype": "Supplier", "fieldname": "custom_x", "label": "X", "fieldtype": "Data"}]
    assert "doctype_broken" in [i.code for i in validate(ChangeSet(ops=ops), snap) if i.level == "error"]


def test_site_facts_listed_in_planner_context():
    from studio.agent import build_context
    erp = FakeErp()
    erp.docs[("Custom Field", "Supplier-custom_a")] = {"name": "Supplier-custom_a", "dt": "Supplier", "fieldname": "custom_a"}
    ctx = build_context("update Supplier records", {}, Snapshot(erp, ttl=0))
    assert "ALREADY BUILT" in ctx and "custom_a" in ctx


def test_web_form_long_text_maps_to_text():
    from studio.compiler import web_form_payload
    from studio.spec import OpAdapter
    op = OpAdapter.validate_python({"op": "create_web_form", "title": "Notes Form", "route": "notes-form", "doctype": "Supplier",
                                    "fields": [{"fieldname": "supplier_name", "fieldtype": "Long Text"}]})
    assert web_form_payload(op)["web_form_fields"][0]["fieldtype"] == "Text"


def test_server_script_format_call_is_rejected():
    got = codes([{"op": "add_server_validation", "name": "fmt", "doctype": "Supplier", "event": "before_save",
                  "script": "if doc.supplier_name == 'x':\n    frappe.throw('Bad {0}'.format(doc.supplier_name))"}])
    assert "py_format" in got
