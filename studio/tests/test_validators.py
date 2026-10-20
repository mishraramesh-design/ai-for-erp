

def test_underscore_names_rejected_in_server_scripts():
    from studio.validators import check_python
    bad = check_python("def _ok(v):\n    return v\n_x = 1\nif _ok(doc.a):\n    frappe.throw('no')", None, False)
    assert any(code == "py_underscore_name" for _, code, _ in bad)
    assert not [f for f in check_python("def ok(v):\n    return v\nif ok(doc.a):\n    frappe.throw('no')", None, False) if f[0] == "error"]


def test_cannot_make_an_autofilled_field_mandatory():
    from studio.spec import ChangeSet
    from studio.validators import validate
    from studio.snapshot import Snapshot
    from tests.fakes import FakeErp

    erp = FakeErp()
    snap = Snapshot(erp, ttl=0)
    orig = snap.meta
    snap.meta = lambda dt: ({**(orig(dt) or {}), "fields": (orig(dt) or {}).get("fields", []) + [
        {"fieldname": "supplier_primary_contact", "fieldtype": "Link", "read_only": 1}]} if dt == "Supplier" else orig(dt))
    cs = ChangeSet(ops=[{"op": "set_property", "doctype": "Supplier", "fieldname": "supplier_primary_contact", "property": "reqd", "value": True}])
    codes = [getattr(i, "code", None) or i.get("code") for i in validate(cs, snap)]
    assert "reqd_autofilled" in codes


def test_top_level_return_is_rejected_but_return_in_def_is_fine():
    from studio.validators import check_python
    assert any(c == "py_top_return" for _, c, _ in check_python("if doc.a:\n    return\nfrappe.throw('x')", None, False))
    assert not any(c == "py_top_return" for _, c, _ in check_python("def ok(v):\n    return v\nif ok(doc.a):\n    frappe.throw('x')", None, False))
