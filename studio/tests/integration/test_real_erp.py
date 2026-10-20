from studio.applier import Applier
from studio.snapshot import Snapshot
from studio.spec import ChangeSet, OpAdapter
from studio.validators import has_errors, order_ops, validate
from studio.verify import verify_changeset


def mk(ops, **kw):
    return ChangeSet(ops=[OpAdapter.validate_python(o) for o in ops], **kw)


def run(erp, ops):
    snap = Snapshot(erp, ttl=0)
    cs = mk(ops)
    cs.ops, _ = order_ops(cs.ops)
    issues = validate(cs, snap)
    assert not has_errors(issues), [i.message for i in issues]
    ok, steps, err = Applier(erp, snap).apply(cs)
    assert ok, err
    cs.steps = steps
    checks = verify_changeset(cs, erp)
    return cs, snap, checks


def failed(checks):
    return [c for c in checks if c.status != 'pass']


def test_meta_and_snapshot_read(erp):
    snap = Snapshot(erp, ttl=0)
    assert snap.has_doctype("Supplier")
    assert "supplier_name" in snap.fieldnames("Supplier")


def test_custom_fields_property_and_undo(erp):
    ops = [
        {"op": "add_custom_field", "doctype": "Supplier", "fieldname": "custom_it_category", "label": "IT Category",
         "fieldtype": "Select", "options": "Domestic\nForeign", "insert_after": "supplier_group"},
        {"op": "add_custom_field", "doctype": "Supplier", "fieldname": "custom_it_year", "label": "IT Year",
         "fieldtype": "Int", "reqd": True},
        {"op": "set_property", "doctype": "Supplier", "fieldname": "custom_it_year", "property": "mandatory_depends_on",
         "value": 'eval:doc.custom_it_category=="Domestic"'},
    ]
    cs, snap, checks = run(erp, ops)
    try:
        assert not failed(checks), [(c.name, c.detail) for c in failed(checks)]
    finally:
        Applier(erp, snap).undo(cs)
    snap.invalidate()
    assert "custom_it_category" not in snap.fieldnames("Supplier")


def test_server_script_and_client_script(erp):
    ops = [
        {"op": "add_server_validation", "doctype": "Supplier", "event": "before_save", "name": "IT supplier check",
         "script": "if doc.supplier_name and doc.supplier_name.strip() == 'BLOCKED':\n    frappe.throw('Blocked name')\n"},
        {"op": "add_client_script", "doctype": "Supplier", "name": "IT supplier client",
         "script": "frappe.ui.form.on('Supplier', { refresh(frm) {} });\n"},
    ]
    cs, snap, checks = run(erp, ops)
    try:
        assert not failed(checks), [(c.name, c.detail) for c in failed(checks)]
        try:
            erp.insert("Supplier", {"supplier_name": "BLOCKED", "supplier_group": "All Supplier Groups"})
            raise AssertionError("script did not reject the record")
        except Exception as e:  # noqa: BLE001
            assert "Blocked name" in str(e)
    finally:
        Applier(erp, snap).undo(cs)


def test_doctype_with_child_role_permission_workflow(erp):
    ops = [
        {"op": "create_role", "role_name": "IT Reviewer"},
        {"op": "create_doctype", "name": "IT Product Row", "is_child_table": True,
         "fields": [{"fieldname": "item_name", "label": "Item Name", "fieldtype": "Data", "reqd": True, "in_list_view": True}]},
        {"op": "create_doctype", "name": "IT Case", "naming": "series", "series_prefix": "ITC-.#####",
         "fields": [{"fieldname": "subject", "label": "Subject", "fieldtype": "Data", "reqd": True},
                    {"fieldname": "rows", "label": "Rows", "fieldtype": "Table", "options": "IT Product Row"}],
         "permissions": [{"role": "IT Reviewer", "write": True, "create": True, "delete": True}]},
        {"op": "create_workflow", "name": "IT Case Flow", "doctype": "IT Case",
         "states": [{"state": "Open", "doc_status": 0, "allow_edit": "IT Reviewer"},
                    {"state": "Closed", "doc_status": 0, "allow_edit": "IT Reviewer"}],
         "transitions": [{"state": "Open", "action": "Close", "next_state": "Closed", "allowed": "IT Reviewer"}]},
    ]
    cs, snap, checks = run(erp, ops)
    try:
        assert not failed(checks), [(c.name, c.detail) for c in failed(checks)]
        doc = erp.insert("IT Case", {"subject": "hello", "rows": [{"item_name": "x"}]})
        assert doc["name"].startswith("ITC-")
        erp.delete("IT Case", doc["name"])
    finally:
        Applier(erp, snap).undo(cs)
    snap.invalidate()
    assert not snap.has_doctype("IT Case")
