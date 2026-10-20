"""Round trip on a real ERPNext: apply -> export -> remove -> import the exported fixtures -> everything is back."""
import json
import os
import subprocess
import uuid

import pytest

from studio.applier import Applier
from studio.exporter import build
from studio.ledger import Ledger
from studio.requirements import RequirementStore
from studio.snapshot import Snapshot
from studio.spec import ChangeSet

BENCH = os.environ.get("STUDIO_IT_BENCH")
SITE = os.environ.get("STUDIO_IT_HOST")
pytestmark = pytest.mark.skipif(not BENCH, reason="STUDIO_IT_BENCH not set")


def test_exported_fixtures_restore_the_change(erp, tmp_path):
    tag = uuid.uuid4().hex[:5]
    field, role, script = f"custom_x{tag}", f"Xrole {tag}", f"Supplier - xcheck {tag}"
    ops = [
        {"op": "create_role", "role_name": role},
        {"op": "add_custom_field", "doctype": "Supplier", "fieldname": field, "label": f"X {tag}", "fieldtype": "Data"},
        {"op": "set_property", "doctype": "Supplier", "fieldname": field, "property": "reqd", "value": 1},
        {"op": "add_server_validation", "doctype": "Supplier", "event": "before_save", "name": f"xcheck {tag}",
         "script": f"if doc.get('{field}') == 'bad':\n    frappe.throw('bad value')"},
        {"op": "add_permission", "doctype": "Supplier", "role": role, "read": True, "write": True},
    ]
    snap = Snapshot(erp, ttl=0)
    ap = Applier(erp, snap)
    cs = ChangeSet(title="x", ops=ops, requirement_ids=[100001], session_id="it")
    ok, steps, err = ap.apply(cs)
    assert ok, err
    cs.steps, cs.status = steps, "applied"
    ledger = Ledger(tmp_path / "l")
    ledger.save(cs)
    reqs = RequirementStore(tmp_path / "r.json")
    reqs.import_text("1. Supplier needs an x field for the round trip test.", "SRM")
    reqs.mark([100001], "built", cs.id)
    reqs.mark([100001], "verified")

    app = os.path.join(BENCH, "apps", "csd_customizations")
    pkg = os.path.join(app, "csd_customizations")
    hooks_path = os.path.join(pkg, "hooks.py")
    original = open(hooks_path).read()
    try:
        out = build(ledger, reqs, original, prefix="")
        for rel, text in out.files.items():
            if rel.startswith("../") or rel.endswith("TRACEABILITY.md"):
                continue
            dest = os.path.join(pkg, rel)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            open(dest, "w").write(text)
        assert not ap.undo(cs)  # remove everything the change created
        assert erp.get_doc("Custom Field", f"Supplier-{field}") is None
        assert erp.get_doc("Server Script", script) is None
        py = (f"import frappe;frappe.init(site='{SITE}',sites_path='.');frappe.connect();"
              "from frappe.utils.fixtures import sync_fixtures;import csd_customizations.studio_apply as a;"
              "sync_fixtures('csd_customizations');a.run();frappe.db.commit()")
        cmd = f"su frappe -c \"cd {BENCH}/sites && ../env/bin/python -c \\\"{py}\\\"\""
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=300)
        assert r.returncode == 0, r.stderr[-1500:]
        assert erp.get_doc("Role", role), "role restored"
        assert erp.get_doc("Custom Field", f"Supplier-{field}"), "custom field restored"
        assert erp.get_doc("Server Script", script), "server script restored"
        perms = erp.list_docs("Custom DocPerm", fields=["name", "write"], filters=[["parent", "=", "Supplier"], ["role", "=", role]], limit=0)
        assert len(perms) == 1 and perms[0]["write"] == 1
    finally:
        open(hooks_path, "w").write(original)
        for d in ("fixtures",):
            subprocess.run(f"rm -rf {os.path.join(pkg, d)} {os.path.join(pkg, 'studio_apply.py')} {os.path.join(pkg, 'studio_manifest.json')}", shell=True)
        for dt, nm in (("Custom Field", f"Supplier-{field}"), ("Server Script", script), ("Role", role)):
            try:
                erp.delete(dt, nm)
            except Exception:
                pass
