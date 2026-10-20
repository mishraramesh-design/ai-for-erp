import os, tempfile
import pytest
from studio.applier import Applier
from studio.ledger import Ledger
from studio.patterns import PatternStore, promote
from studio.router import route
from studio.snapshot import Snapshot
from studio.spec import ChangeSet, OpAdapter
from studio.validators import has_errors, order_ops, validate
from tests.fakes import FakeErp


@pytest.fixture
def env():
    erp = FakeErp()
    snap = Snapshot(erp, ttl=0)
    d = tempfile.mkdtemp()
    return erp, snap, PatternStore(os.path.join(d, "p")), Ledger(os.path.join(d, "l"))


def mk(ops, **kw):
    return ChangeSet(ops=[OpAdapter.validate_python(o) for o in ops], **kw)


def codes(cs, snap):
    return {i.code for i in validate(cs, snap)}


def apply_ok(erp, snap, cs):
    ordered, _ = order_ops(cs.ops); cs.ops = ordered
    assert not has_errors(validate(cs, snap)), [i.message for i in validate(cs, snap)]
    ok, steps, err = Applier(erp, snap).apply(cs)
    assert ok, err
    cs.steps, cs.status = steps, "applied"
    return cs


# ---------------------------------------------------------------- router
def test_rule_adds_field_without_llm(env):
    erp, snap, pats, _ = env
    r = route('Add a mandatory dropdown field "Region" on Sales Order with options North, South', {}, snap, pats, None)
    assert r.kind == "rule" and r.changeset.source == "rule"
    op = r.changeset.ops[0]
    assert op.op == "add_custom_field" and op.doctype == "Sales Order" and op.reqd and op.fieldtype == "Select"
    assert op.fieldname.startswith("custom_")


def test_rule_uses_page_context(env):
    erp, snap, pats, _ = env
    r = route('Add a date field "Review Date"', {"doctype": "Customer"}, snap, pats, None)
    assert r.kind == "rule" and r.changeset.ops[0].doctype == "Customer" and r.changeset.ops[0].fieldtype == "Date"


def test_make_property_rule(env):
    erp, snap, pats, _ = env
    r = route("Make delivery date mandatory on Sales Order", {}, snap, pats, None)
    assert r.kind == "rule" and r.changeset.ops[0].property == "reqd" and r.changeset.ops[0].fieldname == "delivery_date"


def test_lookup_fields(env):
    erp, snap, pats, _ = env
    r = route("what fields does Customer have", {}, snap, pats, None)
    assert r.kind == "lookup" and "credit_limit" in r.reply


def test_unknown_goes_to_llm(env):
    erp, snap, pats, _ = env
    assert route("Block a sales order if the customer exceeded credit by 10%", {"doctype": "Sales Order"}, snap, pats, None).kind == "llm"


def test_replicate_last_change(env):
    erp, snap, pats, _ = env
    cs = apply_ok(erp, snap, mk([{"op": "add_custom_field", "doctype": "Sales Order", "fieldname": "custom_region", "label": "Region", "fieldtype": "Data"}]))
    r = route("do the same for Purchase Order", {}, snap, pats, cs)
    assert r.kind == "replicate" and r.changeset.ops[0].doctype == "Purchase Order"


# ---------------------------------------------------------------- validators
def test_validator_rejects_invented_things(env):
    erp, snap, *_ = env
    cs = mk([{"op": "add_custom_field", "doctype": "Nope DocType", "fieldname": "custom_x", "label": "X", "fieldtype": "Data"}])
    assert has_errors(validate(cs, snap))
    cs = mk([{"op": "add_custom_field", "doctype": "Sales Order", "fieldname": "x_no_prefix", "label": "X", "fieldtype": "Data"}])
    assert has_errors(validate(cs, snap))
    cs = mk([{"op": "add_permission", "doctype": "Sales Order", "role": "Ghost Role"}])
    assert has_errors(validate(cs, snap))
    cs = mk([{"op": "set_setting", "doctype": "User", "field": "x", "value": 1}])
    assert has_errors(validate(cs, snap))


def test_server_script_checks(env):
    erp, snap, *_ = env
    bad = mk([{"op": "add_server_validation", "doctype": "Sales Order", "name": "bad import", "script": "import os\nos.system('ls')"}])
    assert has_errors(validate(bad, snap))
    bad2 = mk([{"op": "add_server_validation", "doctype": "Sales Order", "name": "bad field ref", "script": "if doc.not_a_field > 1:\n    frappe.throw('x')"}])
    assert has_errors(validate(bad2, snap))
    good = mk([{"op": "add_server_validation", "doctype": "Sales Order", "name": "cap", "script": "LIMIT = 100\nif doc.grand_total > LIMIT:\n    frappe.throw('too big')"}])
    assert not has_errors(validate(good, snap)), [i.message for i in validate(good, snap)]


def test_client_script_denylist(env):
    erp, snap, *_ = env
    bad = mk([{"op": "add_client_script", "doctype": "Sales Order", "name": "bad fetch", "script": "frappe.ui.form.on('Sales Order', {refresh(f){ fetch('http://evil') }})"}])
    assert has_errors(validate(bad, snap))
    good = mk([{"op": "add_client_script", "doctype": "Sales Order", "name": "po number mandatory", "script": "frappe.ui.form.on('Sales Order', {refresh(frm){ frm.set_df_property('po_no','reqd',1) }})"}])
    assert not has_errors(validate(good, snap)), [i.message for i in validate(good, snap)]


def test_order_ops_dependencies(env):
    erp, snap, *_ = env
    ops = [
        {"op": "create_doctype", "name": "Vendor Visit", "fields": [{"fieldname": "supplier", "label": "Supplier", "fieldtype": "Link", "options": "Supplier"}],
         "permissions": [{"role": "Visit Officer", "write": True, "create": True}]},
        {"op": "create_role", "role_name": "Visit Officer"},
    ]
    cs = mk(ops)
    ordered, cyc = order_ops(cs.ops)
    assert [o.op for o in ordered] == ["create_role", "create_doctype"] and not cyc
    cs.ops = ordered
    assert not has_errors(validate(cs, snap)), [i.message for i in validate(cs, snap)]
    # permissions added separately on a brand-new doctype are refused (they would replace the standard ones)
    bad = mk([ops[0] | {"permissions": [{"role": "Stock Manager"}]}, {"op": "add_permission", "doctype": "Vendor Visit", "role": "Stock Manager"}])
    bad.ops, _ = order_ops(bad.ops)
    assert "perm_on_new_doctype" in codes(bad, snap)


def test_workflow_validation(env):
    erp, snap, *_ = env
    wf = {"op": "create_workflow", "name": "SO Approval", "doctype": "Sales Order",
          "states": [{"state": "Draft", "doc_status": 0, "allow_edit": "Sales User"},
                     {"state": "Pending", "doc_status": 0, "allow_edit": "Sales Manager"},
                     {"state": "Approved", "doc_status": 1, "allow_edit": "Sales Manager"}],
          "transitions": [{"state": "Draft", "action": "Request Approval", "next_state": "Pending", "allowed": "Sales User"},
                          {"state": "Pending", "action": "Approve", "next_state": "Approved", "allowed": "Sales Manager"}]}
    cs = mk([wf])
    assert not has_errors(validate(cs, snap)), [i.message for i in validate(cs, snap)]
    assert cs.risk() == "high"
    bad = dict(wf, transitions=[{"state": "Draft", "action": "Go", "next_state": "Nowhere", "allowed": "Sales User"}])
    assert has_errors(validate(mk([bad]), snap))
    bad2 = dict(wf, states=[dict(wf["states"][0], doc_status=1)] + wf["states"][1:])
    assert has_errors(validate(mk([bad2]), snap))
    bad3 = dict(wf, doctype="Customer")  # Customer is not submittable but has a submitted state
    assert has_errors(validate(mk([bad3]), snap))


# ---------------------------------------------------------------- applier
def test_apply_and_undo_full_cycle(env):
    erp, snap, *_ = env
    ops = [
        {"op": "create_role", "role_name": "Credit Controller"},
        {"op": "add_custom_field", "doctype": "Customer", "fieldname": "custom_risk", "label": "Risk Class", "fieldtype": "Select", "options": "Low\nHigh"},
        {"op": "set_property", "doctype": "Sales Order", "fieldname": "delivery_date", "property": "reqd", "value": True},
        {"op": "set_setting", "doctype": "Selling Settings", "field": "allow_multiple_items", "value": 1},
        {"op": "add_permission", "doctype": "Customer", "role": "Credit Controller", "read": True, "write": True},
    ]
    cs = apply_ok(erp, snap, mk(ops))
    assert ("Custom Field", "Customer-custom_risk") in erp.docs and ("Role", "Credit Controller") in erp.docs
    assert erp.docs[("Selling Settings", "Selling Settings")]["allow_multiple_items"] == 1
    assert erp.perms and erp.perms[0][3].get("write") == 1
    assert Applier(erp, snap).undo(cs) == []
    assert ("Custom Field", "Customer-custom_risk") not in erp.docs and ("Role", "Credit Controller") not in erp.docs
    assert erp.docs[("Selling Settings", "Selling Settings")].get("allow_multiple_items") in (None, "", 0)
    assert not erp.perms
    assert not [k for k in erp.docs if k[0] == "Property Setter"]


def test_apply_failure_rolls_back_everything(env):
    erp, snap, *_ = env
    erp.fail_on = {"Property Setter"}
    cs = mk([{"op": "add_custom_field", "doctype": "Customer", "fieldname": "custom_a", "label": "A", "fieldtype": "Data"},
             {"op": "set_property", "doctype": "Sales Order", "fieldname": "delivery_date", "property": "reqd", "value": True}])
    ok, steps, err = Applier(erp, snap).apply(cs)
    assert not ok and "rolled back" in err
    assert ("Custom Field", "Customer-custom_a") not in erp.docs


def test_create_doctype_and_workflow_apply(env):
    erp, snap, *_ = env
    cs = apply_ok(erp, snap, mk([
        {"op": "create_doctype", "name": "Vendor Visit", "naming": "series", "fields": [
            {"fieldname": "supplier", "label": "Supplier", "fieldtype": "Link", "options": "Supplier", "reqd": True},
            {"fieldname": "visit_date", "label": "Visit Date", "fieldtype": "Date"}],
         "permissions": [{"role": "Stock Manager", "write": True, "create": True}]}]))
    d = erp.docs[("DocType", "Vendor Visit")]
    assert d["custom"] == 1 and d["autoname"] == "naming_series:" and d["fields"][0]["fieldname"] == "naming_series"
    wf = apply_ok(erp, snap, mk([{"op": "create_workflow", "name": "SO Approval", "doctype": "Sales Order",
        "states": [{"state": "Draft", "doc_status": 0, "allow_edit": "Sales User"}, {"state": "Approved", "doc_status": 1, "allow_edit": "Sales Manager"}],
        "transitions": [{"state": "Draft", "action": "Approve", "next_state": "Approved", "allowed": "Sales Manager"}]}]))
    assert ("Workflow", "SO Approval") in erp.docs and ("Workflow State", "Approved") in erp.docs
    Applier(erp, snap).undo(wf)
    assert ("Workflow", "SO Approval") not in erp.docs and ("Workflow State", "Approved") not in erp.docs


# ---------------------------------------------------------------- patterns
def test_promote_and_reuse_pattern(env):
    erp, snap, pats, _ = env
    script = "LIMIT_PCT = 10\nif doc.grand_total > LIMIT_PCT:\n    frappe.throw('limit')"
    cs = apply_ok(erp, snap, mk([{"op": "add_server_validation", "doctype": "Sales Order", "name": "credit guard", "script": script}],
                                 intent="Block sales order when total exceeds limit 10"))
    p = promote(cs, "Total limit guard", snap.roles())
    pats.save(p)
    assert any(x.name == "LIMIT_PCT" for x in p.params)
    r = route("Block purchase order when total exceeds limit 25", {"doctype": "Purchase Order"}, snap, pats, None)
    assert r.kind == "pattern", r.kind
    assert r.changeset.ops[0].doctype == "Purchase Order" and "LIMIT_PCT = 25" in r.changeset.ops[0].script


def test_openai_empty_answer_on_length_retries_with_bigger_budget(monkeypatch):
    from studio import llm

    calls = []

    class R:
        def __init__(self, content, reason):
            self._d = {"choices": [{"message": {"content": content}, "finish_reason": reason}], "usage": {}}

        def json(self):
            return self._d

    def fake_post(url, headers, body, timeout, retry_codes=()):
        calls.append(body["max_completion_tokens"])
        return R("" if len(calls) == 1 else '{"ok": 1}', "length" if len(calls) == 1 else "stop")

    monkeypatch.setattr(llm, "_post", fake_post)
    out, _ = llm.call_provider("openai", "", "k", "gpt-5", "s", "u")
    assert out == {"ok": 1} and calls[1] == calls[0] * 2
