

def test_underscore_names_rejected_in_server_scripts():
    from studio.validators import check_python
    bad = check_python("def _ok(v):\n    return v\n_x = 1\nif _ok(doc.a):\n    frappe.throw('no')", None, False)
    assert any(code == "py_underscore_name" for _, code, _ in bad)
    assert not [f for f in check_python("def ok(v):\n    return v\nif ok(doc.a):\n    frappe.throw('no')", None, False) if f[0] == "error"]
