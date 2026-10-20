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
