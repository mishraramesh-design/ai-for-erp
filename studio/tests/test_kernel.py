import json
import tempfile
import time

import pytest

from studio.applier import Applier
from studio.kernel import Deps, Kernel, RunStore
from studio.ledger import Ledger
from studio.requirements import RequirementStore
from studio.snapshot import Snapshot
from tests.fakes import FakeErp


class ScriptLlm:
    """Answers each agent by recognising its system prompt. Records every prompt for assertions."""

    def __init__(self, plans, review=None, analyst=None):
        self.plans, self.review, self.analyst = list(plans), list(review or []), analyst
        self.calls, self.prompts = 0, []

    def escalated(self):
        return self

    def complete_json(self, system, user, max_tokens=None):
        self.calls += 1
        self.prompts.append((system[:40], user))
        if "triaging RFP" in system:
            return self.analyst(user) if self.analyst else {"items": []}
        if "closing fit-gap" in system:
            return self.plans.pop(0) if len(self.plans) > 1 else self.plans[0]
        if "strict ERPNext reviewer" in system:
            return self.review.pop(0) if self.review else {"verdicts": []}
        return {"tests": []}


def ids_in(user):
    import re
    return [int(x) for x in re.findall(r"^\[(\d+)\]", user, re.M)]


def plan_supplier_field(ids, extra_ops=()):
    return {"title": "Supplier fields", "summary": "s", "covered": ids, "not_covered": [],
            "ops": [{"op": "add_custom_field", "doctype": "Supplier", "fieldname": "custom_site_visit_date", "label": "Site Visit Date",
                     "fieldtype": "Date"}, *extra_ops]}


@pytest.fixture
def env(tmp_path):
    erp = FakeErp()
    snap = Snapshot(erp, ttl=0)
    reqs = RequirementStore(tmp_path / "r.json")
    reqs.import_text("1. Record the date of each supplier site visit on the supplier master.\n"
                     "2. Prepare a printable print format for the supplier evaluation sheet.\n"
                     "3. Supplier master must support a risk category for each vendor.", "SRM")
    ledger = Ledger(tmp_path / "ledger")
    holder = {"llm": None, "enabled": {"analyst": True, "developer": True, "reviewer": True, "tester": True}}

    def deps():
        return Deps(reqs=reqs, ledger=ledger, erp=erp, snap=snap, applier=Applier(erp, snap),
                    llm_for=lambda role, run_id: holder["llm"], llm_calls=lambda rid: holder["llm"].calls,
                    roles_enabled=lambda r: holder["enabled"].get(r, False))
    k = Kernel(RunStore(tmp_path / "runs"), deps)
    return k, reqs, erp, ledger, holder


def wait_for(k, run, states, timeout=15):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if run.status in states:
            return
        time.sleep(0.05)
    raise AssertionError(f"run stuck in {run.status}: {[(t.id, t.status, t.note) for t in run.tasks]}")


def test_happy_path_rules_then_models_then_verified(env):
    k, reqs, erp, ledger, h = env
    h["llm"] = ScriptLlm([{"x": 0}], review=[{"verdicts": [{"id": 100001, "verdict": "pass", "issue": ""},
                                                          {"id": 100003, "verdict": "pass", "issue": ""}]}])
    h["llm"].plans = [plan_supplier_field([100001, 100003])]
    h["llm"].analyst = lambda u: {"items": [{"id": i, "class": "configure", "reason": "", "approach": "custom field", "question": ""} for i in ids_in(u)]}
    run = k.start("p", [100001, 100002, 100003])
    wait_for(k, run, ("done", "failed"))
    assert run.status == "done", run.error
    assert reqs.get(100002).status == "blocked" and "Print formats" in reqs.get(100002).notes  # set aside by a rule, no model
    assert reqs.get(100001).status == "verified" and reqs.get(100003).status == "verified"
    assert ("Custom Field", "Supplier-custom_site_visit_date") in erp.docs
    pub = k.public(run)
    assert pub["tiers"]["rule"] >= 1 and pub["tiers"]["engine"] >= 2 and pub["tiers"]["model"] >= 2
    assert all(t["status"] in ("done", "skipped") for t in pub["tasks"])
    assert h["llm"].calls == 4  # analyst + developer + reviewer + tester; triage, gate, apply, verify cost nothing


def test_high_risk_waits_for_a_person_then_applies(env):
    k, reqs, erp, ledger, h = env
    plan = plan_supplier_field([100001, 100003], [
        {"op": "create_role", "role_name": "Supplier Evaluator"},
        {"op": "add_permission", "doctype": "Supplier", "role": "Supplier Evaluator", "read": True, "write": True}])
    h["llm"] = ScriptLlm([plan])
    run = k.start("p", [100001, 100003], {"use_analyst": False, "use_reviewer": False})
    wait_for(k, run, ("waiting_approval",))
    assert ("Custom Field", "Supplier-custom_site_visit_date") not in erp.docs  # nothing applied while held
    gate = run.pending_approvals if hasattr(run, "pending_approvals") else [t.id for t in run.tasks if t.status == "held"]
    assert len(gate) == 1 and k.public(run)["tasks"][[t.id for t in run.tasks].index(gate[0])]["tier"] == "human"
    k.approve(run.id, gate[0], "ramesh")
    wait_for(k, run, ("done",))
    assert ("Role", "Supplier Evaluator") in erp.docs and reqs.get(100001).status == "verified"
    assert any("approved by ramesh" in t.note for t in run.tasks)


def test_rejection_returns_requirements_to_backlog(env):
    k, reqs, erp, ledger, h = env
    plan = plan_supplier_field([100001], [{"op": "create_role", "role_name": "X Role"},
                                          {"op": "add_permission", "doctype": "Supplier", "role": "X Role", "read": True}])
    h["llm"] = ScriptLlm([plan])
    run = k.start("p", [100001], {"use_analyst": False, "use_reviewer": False})
    wait_for(k, run, ("waiting_approval",))
    gate = [t.id for t in run.tasks if t.status == "held"][0]
    k.reject(run.id, gate, "ramesh", "too broad")
    wait_for(k, run, ("done",))
    assert reqs.get(100001).status == "backlog" and "too broad" in reqs.get(100001).notes
    assert not [key for key in erp.docs if key[0] == "Custom Field"]
    assert ledger.list()[0].status == "discarded"


def test_reviewer_gap_triggers_repair_with_feedback(env):
    k, reqs, erp, ledger, h = env
    h["llm"] = ScriptLlm([plan_supplier_field([100001]), plan_supplier_field([100001])],
                         review=[{"verdicts": [{"id": 100001, "verdict": "partial", "issue": "officer name is missing"}]},
                                 {"verdicts": [{"id": 100001, "verdict": "pass", "issue": ""}]}])
    run = k.start("p", [100001], {"use_analyst": False})
    wait_for(k, run, ("done",))
    assert reqs.get(100001).status == "verified"
    plan_prompts = [u for s, u in h["llm"].prompts if "solution engineer" in s or "FEEDBACK" in u]
    assert any("officer name is missing" in u for u in plan_prompts)  # the second plan saw the reviewer's complaint
    assert sorted({t.attempt for t in run.tasks if t.kind == "plan"}) == [0, 1]


def test_apply_failure_rolls_back_and_gives_up_after_retries(env):
    k, reqs, erp, ledger, h = env
    erp.fail_on.add("Custom Field")
    h["llm"] = ScriptLlm([plan_supplier_field([100001])])
    run = k.start("p", [100001], {"use_analyst": False, "use_reviewer": False, "max_repairs": 1})
    wait_for(k, run, ("done",))
    assert reqs.get(100001).status == "blocked"
    assert not [key for key in erp.docs if key[0] == "Custom Field"]
    assert ledger.list()[0].status == "failed"
    assert run.groups["g1"]["outcome"] == "blocked"


def test_model_budget_stops_the_run(env):
    k, reqs, erp, ledger, h = env
    h["llm"] = ScriptLlm([plan_supplier_field([100001])])
    h["llm"].calls = 5
    run = k.start("p", [100001], {"use_analyst": False, "max_llm_calls": 5})
    wait_for(k, run, ("done",))
    assert reqs.get(100001).status == "blocked" and any("limit" in t.note for t in run.tasks)


def test_run_survives_restart_and_undo(env, tmp_path):
    k, reqs, erp, ledger, h = env
    h["llm"] = ScriptLlm([plan_supplier_field([100001])])
    run = k.start("p", [100001], {"use_analyst": False, "use_reviewer": False})
    wait_for(k, run, ("done",))
    k2 = Kernel(RunStore(tmp_path / "runs"), k.deps_factory)  # a fresh process reads the same history
    again = k2.get(run.id)
    assert again.status == "done" and len(again.events) == len(run.events) and again.groups["g1"]["cs_id"]
    cs_id = again.groups["g1"]["cs_id"]
    assert k2.undo(cs_id, "ramesh")["ok"]
    assert ("Custom Field", "Supplier-custom_site_visit_date") not in erp.docs and reqs.get(100001).status == "backlog"


def test_nothing_waiting_is_rejected(env):
    k, reqs, *_ = env
    reqs.mark([100001], "verified")
    with pytest.raises(ValueError):
        k.start("p", [100001])
