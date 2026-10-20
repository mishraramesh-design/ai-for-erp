"""Real ERPNext: build notification/report/card/chart/dashboard, verify, export, remove, re-import the fixtures."""
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
from studio.verify import verify_changeset

BENCH = os.environ.get("STUDIO_IT_BENCH")
SITE = os.environ.get("STUDIO_IT_HOST")
pytestmark = pytest.mark.skipif(not BENCH, reason="STUDIO_IT_BENCH not set")


def test_recipes_round_trip(erp, tmp_path):
    t = uuid.uuid4().hex[:5]
    names = {"n": f"Rn {t}", "r": f"Rr {t}", "c": f"Rc {t}", "ch": f"Rch {t}", "d": f"Rd {t}", "h": f"Rh {t}", "w": f"Rw {t}"}
    ops = [
        {"op": "create_notification", "name": names["n"], "doctype": "Supplier", "event": "Save", "subject": "S {{ doc.name }}",
         "message": "<p>{{ doc.supplier_name }}</p>", "condition": "doc.disabled == 1", "recipients": [{"role": "System Manager"}]},
        {"op": "create_report", "name": names["r"], "doctype": "Supplier", "columns": ["name", "supplier_name", "supplier_group"],
         "filters": [{"field": "disabled", "value": 0}]},
        {"op": "create_number_card", "label": names["c"], "doctype": "Supplier", "filters": [{"field": "disabled", "value": 0}]},
        {"op": "create_dashboard_chart", "name": names["ch"], "doctype": "Supplier", "group_by_field": "supplier_group"},
        {"op": "create_html_block", "name": names["h"], "html": "<div class='dp'>demo-pilot</div>", "style": ".dp{background:#FFE600;color:#000;padding:8px}"},
        {"op": "create_dashboard", "name": names["d"], "charts": [names["ch"]], "cards": [names["c"]]},
        {"op": "create_workspace", "title": names["w"], "html_blocks": [names["h"]], "cards": [names["c"]], "charts": [names["ch"]],
         "shortcuts": [{"label": "Suppliers", "doctype": "Supplier"}]},
    ]
    snap = Snapshot(erp, ttl=0)
    ap = Applier(erp, snap)
    cs = ChangeSet(title="recipes", ops=ops, requirement_ids=[100001], session_id="it")
    from studio.validators import has_errors, validate
    issues = validate(cs, snap)
    assert not has_errors(issues), [i for i in issues if i.level == "error"]
    ok, steps, err = ap.apply(cs)
    assert ok, err
    cs.steps, cs.status = steps, "applied"
    bad = [c for c in verify_changeset(cs, erp) if c.status != "pass"]
    assert not bad, bad
    ledger = Ledger(tmp_path / "l")
    ledger.save(cs)
    reqs = RequirementStore(tmp_path / "r.json")
    reqs.import_text("1. Supplier recipes round trip.", "SRM")
    reqs.mark([100001], "built", cs.id)
    reqs.mark([100001], "verified")
    pkg = os.path.join(BENCH, "apps", "csd_customizations", "csd_customizations")
    hooks_path = os.path.join(pkg, "hooks.py")
    original = open(hooks_path).read()
    pairs = [("Notification", names["n"]), ("Report", names["r"]), ("Number Card", names["c"]),
             ("Dashboard Chart", names["ch"]), ("Dashboard", names["d"]),
             ("Custom HTML Block", names["h"]), ("Workspace", names["w"])]
    try:
        out = build(ledger, reqs, original, prefix="")
        for rel, text in out.files.items():
            if rel.startswith("../") or rel.endswith("TRACEABILITY.md"):
                continue
            dest = os.path.join(pkg, rel)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            open(dest, "w").write(text)
        assert not ap.undo(cs)
        for dt, nm in pairs:
            assert erp.get_doc(dt, nm) is None, (dt, nm)
        py = (f"import frappe;frappe.init(site='{SITE}',sites_path='.');frappe.connect();"
              "from frappe.utils.fixtures import sync_fixtures;sync_fixtures('csd_customizations');frappe.db.commit()")
        r = subprocess.run(f"su frappe -c \"cd {BENCH}/sites && ../env/bin/python -c \\\"{py}\\\"\"", shell=True,
                           capture_output=True, text=True, timeout=300)
        assert r.returncode == 0, r.stderr[-1500:]
        for dt, nm in pairs:
            assert erp.get_doc(dt, nm), f"{dt} {nm} restored"
    finally:
        open(hooks_path, "w").write(original)
        subprocess.run(f"rm -rf {os.path.join(pkg, 'fixtures')} {os.path.join(pkg, 'studio_apply.py')} {os.path.join(pkg, 'studio_manifest.json')}", shell=True)
        for dt, nm in reversed(pairs):
            try:
                erp.delete(dt, nm)
            except Exception:
                pass
