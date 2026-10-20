"""The kernel end to end on a REAL ERPNext site, with a scripted model (model quality is not under test here).
Covers: rules, plan, review, auto gate, apply, read-back verification, a behavioural test, a held high-risk gate, undo."""
import time
import uuid

import pytest

from studio.applier import Applier
from studio.kernel import Deps, Kernel, RunStore
from studio.ledger import Ledger
from studio.requirements import RequirementStore
from studio.snapshot import Snapshot
from tests.test_kernel import ScriptLlm, wait_for


def test_build_verify_approve_undo_on_real_site(erp, tmp_path):
    tag = uuid.uuid4().hex[:5]
    field, role = f"custom_it_{tag}", f"IT Auditor {tag}"
    reqs = RequirementStore(tmp_path / "r.json")
    reqs.import_text(f"1. Record the integration visit date on every supplier {tag}.\n2. Only the {role} role may edit supplier risk data.", "IT")
    a, b = sorted(r.id for r in reqs.all())
    llm = ScriptLlm([{"title": "Visit date", "summary": "s", "covered": [a], "not_covered": [], "ops": [
        {"op": "add_custom_field", "doctype": "Supplier", "fieldname": field, "label": f"Visit {tag}", "fieldtype": "Date"}]}])
    snap = Snapshot(erp, ttl=0)
    ledger = Ledger(tmp_path / "ledger")
    holder = {"llm": llm}
    deps = lambda: Deps(reqs=reqs, ledger=ledger, erp=erp, snap=snap, applier=Applier(erp, snap),
                        llm_for=lambda role_, rid: holder["llm"], llm_calls=lambda rid: holder["llm"].calls,
                        roles_enabled=lambda r: r in ("developer", "reviewer"))
    k = Kernel(RunStore(tmp_path / "runs"), deps)
    opts = {"use_analyst": False, "use_tester": False}

    holder["llm"].review = [{"verdicts": [{"id": a, "verdict": "pass", "issue": ""}]}]
    run = k.start("it", [a], opts)
    wait_for(k, run, ("done", "failed"), timeout=120)
    assert run.status == "done" and reqs.get(a).status == "verified", [(t.id, t.status, t.note) for t in run.tasks]
    assert erp.get_doc("Custom Field", f"Supplier-{field}")

    llm2 = ScriptLlm([{"title": "Auditor", "summary": "s", "covered": [b], "not_covered": [], "ops": [
        {"op": "create_role", "role_name": role},
        {"op": "add_permission", "doctype": "Supplier", "role": role, "read": True, "write": True}]}],
        review=[{"verdicts": [{"id": b, "verdict": "pass", "issue": ""}]}])
    holder["llm"] = llm2
    run2 = k.start("it", [b], opts)
    wait_for(k, run2, ("waiting_approval",), timeout=60)
    assert not erp.get_doc("Role", role)  # nothing applied while the gate is held
    k.approve(run2.id, [t.id for t in run2.tasks if t.status == "held"][0], "it-user")
    wait_for(k, run2, ("done",), timeout=120)
    assert reqs.get(b).status == "verified" and erp.get_doc("Role", role)

    for cs_id in (run2.groups["g1"]["cs_id"], run.groups["g1"]["cs_id"]):
        assert k.undo(cs_id, "it-user")["ok"]
    assert not erp.get_doc("Role", role) and not erp.get_doc("Custom Field", f"Supplier-{field}")
    assert reqs.get(a).status == "backlog" and reqs.get(b).status == "backlog"
