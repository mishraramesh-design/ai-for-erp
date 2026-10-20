import json
import re

import pytest

from studio.applier import Applier
from studio.exporter import BLOCK_END, BLOCK_START, build, splice_hooks
from studio.ledger import Ledger
from studio.requirements import RequirementStore
from studio.snapshot import Snapshot
from studio.spec import ChangeSet
from tests.fakes import FakeErp

HOOKS = f'app_name = "csd_customizations"\n\n{BLOCK_START}\nfixtures = []\n{BLOCK_END}\n'


def make(tmp_path, ops, verified=True):
    erp = FakeErp()
    snap = Snapshot(erp, ttl=0)
    reqs = RequirementStore(tmp_path / "r.json")
    reqs.import_text("1. Supplier master must record a vendor risk rating with review date.", "SRM")
    ledger = Ledger(tmp_path / "l")
    cs = ChangeSet(title="Risk rating", ops=ops, requirement_ids=[100001], session_id="run1")
    ok, steps, err = Applier(erp, snap).apply(cs)
    assert ok, err
    cs.steps, cs.status = steps, "applied"
    ledger.save(cs)
    reqs.mark([100001], "built", cs.id)
    if verified:
        reqs.mark([100001], "verified")
    return ledger, reqs, cs


OPS = [
    {"op": "add_custom_field", "doctype": "Supplier", "fieldname": "custom_risk", "label": "Risk", "fieldtype": "Select",
     "options": "Low\nHigh", "reqd": True},
    {"op": "set_property", "doctype": "Supplier", "fieldname": "custom_risk", "property": "reqd", "value": 1},
    {"op": "add_server_validation", "doctype": "Supplier", "event": "before_save", "name": "check risk",
     "script": "if doc.custom_risk == 'High':\n    frappe.throw('x')"},
    {"op": "create_role", "role_name": "Risk Officer"},
    {"op": "add_permission", "doctype": "Supplier", "role": "Risk Officer", "read": True, "write": True},
]


def test_export_files_and_determinism(tmp_path):
    ledger, reqs, cs = make(tmp_path, OPS)
    a = build(ledger, reqs, HOOKS)
    b = build(ledger, reqs, HOOKS)
    assert a.files == b.files  # deterministic
    p = "customizations/csd_customizations/csd_customizations/"
    assert json.loads(a.files[p + "fixtures/custom_field.json"])[0]["fieldname"] == "custom_risk"
    assert json.loads(a.files[p + "fixtures/server_script.json"])[0]["script_type"] == "DocType Event"
    assert json.loads(a.files[p + "fixtures/role.json"])[0]["name"] == "Risk Officer"
    assert "Risk Officer" in a.files[p + "studio_apply.py"]
    hooks = a.files[p + "hooks.py"]
    ns = {}
    exec(hooks, ns)  # the generated block is valid Python
    assert [f["dt"] for f in ns["fixtures"]] == ["Role", "Custom Field", "Property Setter", "Server Script"]
    assert ns["after_migrate"] == ["csd_customizations.studio_apply.run"]
    assert "customizations/csd_customizations/TRACEABILITY.md" in a.files
    assert "Requirement 100001" in a.files["customizations/csd_customizations/TRACEABILITY.md"]
    json.loads(a.files[p + "studio_manifest.json"])


def test_unverified_and_undone_change_sets_are_not_exported(tmp_path):
    ledger, reqs, cs = make(tmp_path, OPS, verified=False)
    e = build(ledger, reqs, HOOKS)
    assert e.change_sets == [] and e.skipped and "verified" in e.skipped[0]["reason"]
    reqs.mark([100001], "verified")
    assert build(ledger, reqs, HOOKS).change_sets == [cs.id]
    cs.status = "undone"
    ledger.save(cs)
    assert build(ledger, reqs, HOOKS).change_sets == []


def test_splice_keeps_the_rest_of_hooks():
    out = splice_hooks(HOOKS, f"{BLOCK_START}\nfixtures = [1]\n{BLOCK_END}")
    assert out.startswith('app_name = "csd_customizations"') and "fixtures = [1]" in out and out.count(BLOCK_START) == 1
    assert "GENERATED" in splice_hooks("x = 1\n", f"{BLOCK_START}\n{BLOCK_END}")


def test_autofill_fills_other_changes_mandatory_fields_but_not_own():
    from studio.verify import autofill

    class E:
        def get_meta(self, dt):
            return {"fields": [
                {"fieldname": "supplier_name", "fieldtype": "Data", "reqd": 1},
                {"fieldname": "primary_contact", "fieldtype": "Link", "options": "Contact", "reqd": 1},
                {"fieldname": "custom_risk", "fieldtype": "Select", "options": "Low\nHigh", "reqd": 1},
                {"fieldname": "kind", "fieldtype": "Select", "options": "\nA\nB", "reqd": 1}]}

        def list_docs(self, dt, fields=None, filters=None, limit=0):
            return []

        def insert(self, dt, data):
            return {"name": "C-1"}

    made = []
    out = autofill(E(), "Supplier", {"supplier_name": "X"}, {"custom_risk"}, made)
    assert out["supplier_name"] == "X" and out["primary_contact"] == "C-1" and out["kind"] == "A"
    assert "custom_risk" not in out and made == [("Contact", "C-1")]
