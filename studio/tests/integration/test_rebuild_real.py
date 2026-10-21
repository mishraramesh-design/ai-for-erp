"""A custom DocType whose database table went missing is rebuilt by a create_doctype op with rebuild=true."""
import json
import os
import uuid

import pytest

from studio.applier import Applier
from studio.snapshot import Snapshot
from studio.spec import ChangeSet
from studio.validators import has_errors, validate
from studio.verify import verify_changeset

BENCH = os.environ.get("STUDIO_IT_BENCH")
SITE = os.environ.get("STUDIO_IT_HOST")
pytestmark = pytest.mark.skipif(not BENCH, reason="STUDIO_IT_BENCH not set")


def test_rebuild_recreates_missing_table(erp):
    import pymysql
    name = f"Zz Rebuild {uuid.uuid4().hex[:4]}"
    op = {"op": "create_doctype", "name": name, "naming": "hash", "fields": [{"fieldname": "title", "label": "Title", "fieldtype": "Data"}],
          "permissions": [{"role": "System Manager", "read": True, "write": True, "create": True}]}
    snap = Snapshot(erp, ttl=0)
    ap = Applier(erp, snap)
    cs = ChangeSet(title="make", ops=[op], requirement_ids=[100002], session_id="it")
    ok, steps, err = ap.apply(cs)
    assert ok, err
    cfg = json.load(open(os.path.join(BENCH, "sites", SITE, "site_config.json")))
    db = pymysql.connect(host="127.0.0.1", user=cfg["db_name"], password=cfg["db_password"], database=cfg["db_name"])
    db.cursor().execute(f"drop table `tab{name}`")
    db.commit()
    import subprocess  # Frappe caches the list of tables; an out-of-band drop needs the cache cleared, as a real incident would
    subprocess.run(f"su frappe -c 'cd {BENCH} && bench --site {SITE} clear-cache'", shell=True, check=True, capture_output=True)
    with pytest.raises(Exception):
        erp.list_docs(name, ["name"])
    plain = ChangeSet(title="again", ops=[op], requirement_ids=[100002], session_id="it")
    assert has_errors(validate(plain, Snapshot(erp, ttl=0)))  # without rebuild it is refused
    fixed = ChangeSet(title="fix", ops=[{**op, "rebuild": True, "fields": op["fields"] + [{"fieldname": "note", "label": "Note", "fieldtype": "Data"}]}],
                      requirement_ids=[100002], session_id="it")
    issues = validate(fixed, Snapshot(erp, ttl=0))
    assert not has_errors(issues), issues
    ok, steps, err = Applier(erp, Snapshot(erp, ttl=0)).apply(fixed)
    assert ok, err
    fixed.steps, fixed.status = steps, "applied"
    assert erp.list_docs(name, ["name"]) == []
    assert not [c for c in verify_changeset(fixed, erp) if c.status != "pass"]
    erp.delete("DocType", name)
