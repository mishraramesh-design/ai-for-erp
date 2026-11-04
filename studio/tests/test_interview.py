from studio.interview import InterviewStore, apply_reply, approve, brd_markdown, build_prompt, turn
from studio.llm import LlmError
from studio.requirements import RequirementStore


class FakeSnap:
    def __init__(self):
        self._d = {"Supplier": {"module": "Buying", "istable": 0, "custom": 0}, "Bank Guarantee": {"module": "Accounts", "istable": 0, "custom": 0},
                   "Purchase Order": {"module": "Buying", "istable": 0, "custom": 0}}

    def doctype_names(self):
        return sorted(self._d)

    def doctypes(self):
        return self._d

    def meta(self, n):
        return {"module": self._d[n]["module"], "is_submittable": n == "Purchase Order", "custom": False,
                "fields": [{"fieldname": "supplier_name", "label": "Supplier Name", "fieldtype": "Data"}]}

    def site_facts(self, n):
        return []


class FakeLlm:
    def __init__(self, out=None, err=None):
        self.out, self.err, self.calls = out, err, []

    def complete_json(self, system, user, max_tokens=None):
        self.calls.append((system, user))
        if self.err:
            raise LlmError(self.err)
        return self.out


def mk(tmp_path):
    st = InterviewStore(tmp_path / "iv.json")
    return st, st.create("SRM workshop", "Supplier Relationship Management")


REPLY = {"reply": "Bank Guarantee exists in ERPNext.", "requirements": [
    {"id": None, "text": "Track bank guarantee expiry per supplier and alert 30 days before", "process": "Bank Guarantee", "priority": "Must",
     "fit": "Standard", "doctypes": ["Bank Guarantee"], "rationale": "Bank Guarantee has validity dates.", "acceptance": ["Alert sent 30 days before"]},
    {"id": None, "text": "Suppliers upload turnover certificate on the portal", "process": "Registration", "priority": "Should",
     "fit": "Standard", "doctypes": ["Made Up Doctype"], "rationale": "x"},
    {"id": None, "text": "Send SMS OTP on registration", "process": "Registration", "priority": "Could", "fit": "Not possible",
     "doctypes": [], "workaround": "Use an SMS gateway app"}],
    "questions": [{"id": None, "text": "Which categories need a guarantee?", "options": ["All", "Works only"], "suggested": "Works only", "why": "Typical", "blocks": ["R1"]}]}


def test_turn_builds_drafts_questions_and_checks_doctypes(tmp_path):
    st, s = mk(tmp_path)
    ch = turn(s, "Client wants to track bank guarantee expiry", FakeLlm(REPLY), FakeSnap())
    assert ch["added"] == 3 and ch["questions"] == 1
    r1, r2, r3 = s.drafts
    assert r1.fit == "Standard" and r1.doctypes == ["Bank Guarantee"] and not r1.unverified
    assert r2.fit == "Configuration" and r2.unverified and r2.doctypes == []   # unknown DocType cannot back 'Standard'
    assert r3.fit == "Not possible"
    assert s.questions[0].blocks == ["R1"] and s.questions[0].suggested == "Works only"
    assert [m.role for m in s.messages] == ["ba", "agent"]


def test_no_site_never_claims_standard_as_verified(tmp_path):
    st, s = mk(tmp_path)
    turn(s, "bank guarantee", FakeLlm(REPLY), None)
    assert all(d.unverified or d.fit not in ("Standard", "Configuration") for d in s.drafts)


def test_update_by_id_and_answers_and_duplicates(tmp_path):
    st, s = mk(tmp_path)
    turn(s, "a", FakeLlm(REPLY), FakeSnap())
    again = {"requirements": [{"id": "R1", "text": "Track bank guarantee expiry and alert 45 days before", "fit": "Configuration", "doctypes": ["Bank Guarantee"]},
                              {"id": None, "text": "Send SMS OTP on registration"}],
             "answered": [{"id": "Q1", "answer": "Works only"}], "dropped": ["R3"]}
    ch = turn(s, "make it 45 days", FakeLlm(again), FakeSnap())
    assert ch["updated"] == 1 and ch["added"] == 0 and ch["answered"] == 1 and ch["dropped"] == 1
    assert "45 days" in s.drafts[0].text and s.questions[0].status == "answered" and s.drafts[2].status == "rejected"


def test_model_failure_keeps_the_ba_message(tmp_path):
    st, s = mk(tmp_path)
    out = turn(s, "hello", FakeLlm(err="boom"), FakeSnap())
    assert "error" in out and s.messages[0].role == "ba" and "boom" in s.messages[1].text


def test_approve_enters_backlog_with_fit_mapping_and_freezes(tmp_path):
    st, s = mk(tmp_path)
    turn(s, "a", FakeLlm(REPLY), FakeSnap())
    reqs = RequirementStore(tmp_path / "r.json")
    res = approve(s, ["R1", "R3"], reqs, "ba@x")
    got = {r["draft"]: reqs.get(r["requirement"]) for r in res["approved"]}
    assert got["R1"].status == "fit" and got["R1"].fit == "Standard" and got["R1"].approved_by == "ba@x"
    assert got["R3"].status == "blocked" and "SMS gateway" in got["R3"].remarks
    assert res["open_questions_on_these"] == ["Q1"]
    assert approve(s, ["R1"], reqs, "ba@x")["approved"] == []                    # no double approval
    again = turn(s, "x", FakeLlm({"requirements": [{"id": "R1", "text": "changed"}]}), FakeSnap())
    assert again["updated"] == 0 and s.drafts[0].text.startswith("Track bank")   # approved is frozen
    cfg = approve(s, ["R2"], reqs, "ba@x")["approved"][0]
    assert reqs.get(cfg["requirement"]).status == "backlog" and reqs.get(cfg["requirement"]).is_gap


def test_prompt_contains_real_doctypes_and_packs(tmp_path):
    st, s = mk(tmp_path)
    _, user = build_prompt(s, "supplier bank guarantee validity", FakeSnap())
    assert "Bank Guarantee [Accounts" in user and "Supplier Name (Data)" in user
    assert "Standard: ERPNext already does it" in user and "Procurement and Supplier" in user


def test_brd_has_flows_and_sections(tmp_path):
    st, s = mk(tmp_path)
    turn(s, "a", FakeLlm(REPLY), FakeSnap())
    md = brd_markdown(s)
    assert "```mermaid" in md
    assert "## 4. Requirements" in md and "Open questions" in md and "R3" in md


def test_persistence(tmp_path):
    st, s = mk(tmp_path)
    turn(s, "a", FakeLlm(REPLY), FakeSnap())
    st.save()
    st2 = InterviewStore(tmp_path / "iv.json")
    assert len(st2.get(s.id).drafts) == 3


def test_api_flow(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from studio.app import create_app
    monkeypatch.setenv("STUDIO_ADMIN_PASSWORD", "pw123")
    app = create_app(str(tmp_path))
    c = TestClient(app)
    assert c.get("/api/interviews").status_code == 401
    c.post("/api/login", json={"password": "pw123"})
    sid = c.post("/api/interviews", json={"title": "W", "module": "SRM"}).json()["id"]
    assert c.post(f"/api/interviews/{sid}/message", json={"text": "hi"}).status_code == 409   # no model configured
    c.put("/api/models", json={"api_key": "k", "cheap": "m", "strong": "m", "profile": "balanced"})
    import studio.models as M
    app.state.router.caller = lambda *a, **k: (REPLY, {"in": 1, "out": 1})
    r = c.post(f"/api/interviews/{sid}/message", json={"text": "bank guarantee"}).json()
    assert len(r["drafts"]) == 3 and r["open_questions"] == 1
    assert c.post(f"/api/interviews/{sid}/drafts/R1", json={"priority": "Could"}).json()["drafts"][0]["priority"] == "Could"
    assert c.post(f"/api/interviews/{sid}/questions/Q1", json={"answer": "Works only"}).json()["open_questions"] == 0
    ap = c.post(f"/api/interviews/{sid}/approve", json={"ids": ["R1"]}).json()
    assert ap["approved"][0]["status"] == "fit"
    assert c.post(f"/api/interviews/{sid}/drafts/R1", json={"text": "x"}).status_code == 409
    assert "# Business Requirement Document" in c.get(f"/api/interviews/{sid}/brd").json()["markdown"]
    assert c.get("/api/requirements?gap=false&limit=5").json()["total"] >= 1
