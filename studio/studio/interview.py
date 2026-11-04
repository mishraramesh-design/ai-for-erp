"""Requirement-gathering interview: a BA talks, requirements build up, each judged against the real ERPNext site.

The model proposes; this module validates and records. Facts about ERPNext (does this DocType exist, which fields it has)
come from the live Snapshot, never from the model's memory. Nothing enters the build backlog until a BA approves it.
"""
from __future__ import annotations

import json
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

from .llm import LlmError

FITS = ("Standard", "Configuration", "Customization", "Not possible")
PRIORITIES = ("Must", "Should", "Could")
PACK_DIR = Path(__file__).resolve().parent / "packs"
MAX_TEXT = 600


class Draft(BaseModel):
    id: str
    text: str
    process: str = ""
    priority: str = "Should"
    fit: str = "Customization"
    doctypes: list[str] = Field(default_factory=list)  # confirmed to exist on the dev site
    rationale: str = ""
    workaround: str = ""
    acceptance: list[str] = Field(default_factory=list)
    unverified: bool = False  # the fit claim names no DocType that exists on the site
    status: str = "draft"  # draft | approved | rejected
    req_id: Optional[int] = None


class Question(BaseModel):
    id: str
    text: str
    options: list[str] = Field(default_factory=list)
    suggested: str = ""
    why: str = ""
    blocks: list[str] = Field(default_factory=list)  # draft ids that wait on the answer
    status: str = "open"  # open | answered | dropped
    answer: str = ""


class Message(BaseModel):
    role: str  # ba | agent
    text: str
    ts: float


class Session(BaseModel):
    id: str
    title: str
    module: str
    created: float
    messages: list[Message] = Field(default_factory=list)
    drafts: list[Draft] = Field(default_factory=list)
    questions: list[Question] = Field(default_factory=list)
    counters: dict = Field(default_factory=lambda: {"R": 0, "Q": 0})


class InterviewStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._items: dict[str, Session] = {}
        if self.path.exists():
            try:
                for d in json.loads(self.path.read_text(encoding="utf-8")):
                    s = Session.model_validate(d)
                    self._items[s.id] = s
            except Exception:
                self._items = {}

    def save(self) -> None:
        with self._lock:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps([s.model_dump(mode="json") for s in self._items.values()]), encoding="utf-8")
            tmp.replace(self.path)

    def create(self, title: str, module: str) -> Session:
        with self._lock:
            s = Session(id=uuid.uuid4().hex[:8], title=title.strip() or "Requirement workshop", module=module.strip() or "General",
                        created=time.time())
            self._items[s.id] = s
            self.save()
            return s

    def get(self, sid: str) -> Optional[Session]:
        return self._items.get(sid)

    def list(self) -> list[Session]:
        return sorted(self._items.values(), key=lambda s: -s.created)

    @property
    def lock(self):
        return self._lock


# ---------------------------------------------------------------------------------------------- knowledge
def _words(text: str) -> set:
    return {w for w in re.findall(r"[a-z]{4,}", (text or "").lower()) if w not in {"that", "this", "with", "from", "have", "should", "must", "shall", "will", "when", "they", "their", "system", "which", "into", "than", "each", "also", "need", "want"}}


def load_packs(text: str, module: str, extra_dir: Optional[Path] = None, budget: int = 7000) -> str:
    """Domain knowledge files: the core pack always, others when their keywords appear in the conversation or module."""
    hay = (text + " " + module).lower()
    chunks: list[str] = []
    dirs = [PACK_DIR] + ([extra_dir] if extra_dir and extra_dir.exists() else [])
    for d in dirs:
        for f in sorted(d.glob("*.md")):
            raw = f.read_text(encoding="utf-8", errors="ignore")
            m = re.match(r"^---\s*\nkeywords:\s*(.*?)\n---\s*\n", raw, re.S)
            kws = [k.strip().lower() for k in (m.group(1).split(",") if m else []) if k.strip()]
            body = raw[m.end():] if m else raw
            if f.stem == "erpnext_core" or any(k in hay for k in kws):
                chunks.append(body.strip())
    out = "\n\n".join(chunks)
    return out[:budget]


def ground(snap, text: str, limit: int = 6) -> tuple[str, set]:
    """The ERPNext DocTypes most related to the conversation, with their real fields. Returns (context text, names)."""
    if snap is None:
        return "ERPNext site is not connected: do not claim anything is Standard; mark fit as unverified.", set()
    try:
        names = snap.doctype_names()
        info = snap.doctypes()
    except Exception as e:  # noqa: BLE001 - a missing site must not break the chat
        return f"ERPNext site could not be read ({e}): do not claim anything is Standard.", set()
    want = _words(text)
    scored = []
    for n in names:
        row = info.get(n, {})
        if row.get("istable"):
            continue
        nw = _words(n)
        hit = len(nw & want)
        if n.lower() in text.lower():
            hit += 3
        if hit:
            scored.append((hit + (0.5 if row.get("custom") else 0), n))
    scored.sort(key=lambda x: (-x[0], x[1]))
    top = [n for _, n in scored[:limit]]
    lines = []
    for n in top:
        try:
            meta = snap.meta(n) or {}
        except Exception:  # noqa: BLE001
            continue
        fs = [f"{f.get('label') or f['fieldname']} ({f['fieldtype']})" for f in meta.get("fields", [])
              if f.get("fieldname") and f.get("fieldtype") not in ("Section Break", "Column Break", "Tab Break", "HTML", "Button")][:22]
        flags = [x for x in ("submittable" if meta.get("is_submittable") else "", "custom" if meta.get("custom") else "") if x]
        facts: list[str] = []
        if meta.get("custom"):
            try:
                facts = snap.site_facts(n)
            except Exception:  # noqa: BLE001
                facts = []
        lines.append(f"- {n} [{meta.get('module', '')}{', ' + ', '.join(flags) if flags else ''}]: " + ", ".join(fs)
                     + (" | already built: " + "; ".join(facts) if facts else ""))
    ctx = "ERPNext DocTypes on the development site that look related (real names and fields):\n" + ("\n".join(lines) if lines else "- none matched; ask the BA which ERPNext area this belongs to")
    return ctx, set(top) | set(names)


# ---------------------------------------------------------------------------------------------- prompt
SYSTEM = """You are a senior ERPNext v15 functional consultant and business analyst running a requirement workshop with a BA.
The BA relays what the client says. Your job: turn it into precise, testable requirements, judge each against REAL ERPNext
capability, and ask the questions a seasoned consultant would ask next, always offering suggested answers.

Reply with ONE JSON object:
{"reply": str,
 "requirements": [{"id": str|null, "text": str, "process": str, "priority": "Must|Should|Could",
                   "fit": "Standard|Configuration|Customization|Not possible", "doctypes": [str], "rationale": str,
                   "workaround": str, "acceptance": [str]}],
 "questions": [{"id": str|null, "text": str, "options": [str], "suggested": str, "why": str, "blocks": [requirement ids]}],
 "answered": [{"id": str, "answer": str}],
 "dropped": [requirement ids to remove]}

Rules:
- "reply" is short and conversational (max 120 words). Say what you understood, which ERPNext feature or DocType you relied on, and what you still need. Do not repeat every requirement.
- One atomic requirement per entry: one actor, one action, one outcome, testable. Split compound statements. Rewrite vague wording into precise wording but keep the client's intent.
- To change an existing requirement, send it again with its id. New ones have id null. Never duplicate an existing one.
- fit: Standard only if an existing ERPNext feature does it with master data only, and you name that DocType in "doctypes" taken from the ERPNext context below. Configuration for settings, custom fields, workflow, permissions, notifications, reports, print formats. Customization for scripts or a new DocType. Not possible for external systems, hardware or native apps, and then give a workaround or integration idea in "workaround".
- "rationale": one sentence on why that fit, naming the ERPNext feature. "acceptance": 2 to 4 short checks a tester can run.
- Ask at most 3 questions per turn, only ones that change the requirement or the fit. Each has 2 to 4 concrete options and your suggested option with a one-line "why". "blocks" lists requirement ids that cannot be finished until it is answered.
- If the BA's message answers an open question, put it in "answered" with the id and answer.
- Use only DocType names that appear in the ERPNext context. Never invent fields. If unsure whether ERPNext has a feature, say so and choose Customization or ask.
- Never output anything except the JSON object."""


def _clip(t: str, n: int = MAX_TEXT) -> str:
    return re.sub(r"\s+", " ", str(t or "")).strip()[:n]


def _state_text(s: Session) -> str:
    dr = [f"{d.id} [{d.fit}{'*' if d.unverified else ''}/{d.priority}/{d.status}] {d.process}: {d.text}" for d in s.drafts if d.status != "rejected"]
    qs = [f"{q.id} [{q.status}] {q.text}" + (f" -> {q.answer}" if q.answer else "") for q in s.questions if q.status != "dropped"]
    return "REQUIREMENTS SO FAR:\n" + ("\n".join(dr) or "none") + "\n\nQUESTIONS:\n" + ("\n".join(qs) or "none")


def build_prompt(s: Session, text: str, snap, extra_dir: Optional[Path] = None) -> tuple[str, str]:
    recent = " ".join(m.text for m in s.messages[-6:])
    corpus = recent + " " + text + " " + " ".join(d.text for d in s.drafts)
    erp_ctx, _ = ground(snap, corpus + " " + s.module)
    packs = load_packs(corpus, s.module, extra_dir)
    hist = "\n".join(f"{'BA' if m.role == 'ba' else 'ASSISTANT'}: {_clip(m.text, 900)}" for m in s.messages[-10:])
    user = (f"MODULE UNDER DISCUSSION: {s.module}\n\nDOMAIN KNOWLEDGE:\n{packs}\n\n{erp_ctx}\n\n{_state_text(s)}\n\n"
            f"RECENT CONVERSATION:\n{hist or '(start)'}\n\nNEW MESSAGE FROM THE BA:\n{_clip(text, 4000)}")
    return SYSTEM, user


# ---------------------------------------------------------------------------------------------- apply the model's answer
def apply_reply(s: Session, out: dict, known_doctypes: set, snapshot_ok: bool) -> dict:
    """Validate and record. Returns counters for the UI."""
    changed = {"added": 0, "updated": 0, "questions": 0, "answered": 0, "dropped": 0}
    by_id = {d.id: d for d in s.drafts}
    for r in (out.get("requirements") or [])[:12]:
        if not isinstance(r, dict) or not _clip(r.get("text")):
            continue
        fit = r.get("fit") if r.get("fit") in FITS else "Customization"
        dts = [x for x in (r.get("doctypes") or []) if isinstance(x, str)]
        real = [x for x in dts if x in known_doctypes]
        unverified = fit in ("Standard", "Configuration") and not real
        if fit == "Standard" and not real:
            fit = "Configuration" if snapshot_ok else fit  # an unnamed or unknown DocType cannot back a 'Standard' claim
        fields = dict(text=_clip(r.get("text")), process=_clip(r.get("process"), 80), fit=fit,
                      priority=r.get("priority") if r.get("priority") in PRIORITIES else "Should", doctypes=real[:6],
                      rationale=_clip(r.get("rationale"), 400), workaround=_clip(r.get("workaround"), 400),
                      acceptance=[_clip(a, 200) for a in (r.get("acceptance") or []) if _clip(a)][:5], unverified=unverified)
        rid = r.get("id")
        if rid in by_id and by_id[rid].status != "approved":
            for k, v in fields.items():
                setattr(by_id[rid], k, v)
            changed["updated"] += 1
        elif rid in by_id:
            continue  # an approved requirement is frozen; the BA reopens it explicitly
        else:
            if any(d.text.lower() == fields["text"].lower() for d in s.drafts):
                continue
            s.counters["R"] += 1
            d = Draft(id=f"R{s.counters['R']}", **fields)
            s.drafts.append(d)
            by_id[d.id] = d
            changed["added"] += 1
    qid = {q.id: q for q in s.questions}
    for q in (out.get("questions") or [])[:3]:
        if not isinstance(q, dict) or not _clip(q.get("text")):
            continue
        if any(x.text.lower() == _clip(q.get("text")).lower() for x in s.questions):
            continue
        s.counters["Q"] += 1
        opts = [_clip(o, 120) for o in (q.get("options") or []) if _clip(o)][:4]
        sug = _clip(q.get("suggested"), 120)
        nq = Question(id=f"Q{s.counters['Q']}", text=_clip(q.get("text"), 300), options=opts, suggested=sug,
                      why=_clip(q.get("why"), 240), blocks=[b for b in (q.get("blocks") or []) if b in by_id])
        s.questions.append(nq)
        qid[nq.id] = nq
        changed["questions"] += 1
    for a in (out.get("answered") or []):
        if isinstance(a, dict) and a.get("id") in qid and qid[a["id"]].status == "open" and _clip(a.get("answer")):
            qid[a["id"]].status, qid[a["id"]].answer = "answered", _clip(a.get("answer"), 400)
            changed["answered"] += 1
    for did in (out.get("dropped") or []):
        if did in by_id and by_id[did].status == "draft":
            by_id[did].status = "rejected"
            changed["dropped"] += 1
    return changed


def turn(s: Session, text: str, llm, snap, extra_dir: Optional[Path] = None) -> dict:
    """One conversational turn. The user message is stored first, so it survives a model failure."""
    s.messages.append(Message(role="ba", text=_clip(text, 4000), ts=time.time()))
    system, user = build_prompt(s, text, snap, extra_dir)
    try:
        out = llm.complete_json(system, user, max_tokens=4500)
    except LlmError as e:
        s.messages.append(Message(role="agent", text=f"I could not answer: {e}", ts=time.time()))
        return {"error": str(e)}
    known: set = set()
    ok = snap is not None
    if ok:
        try:
            known = set(snap.doctype_names())
        except Exception:  # noqa: BLE001
            ok = False
    ch = apply_reply(s, out, known, ok)
    s.messages.append(Message(role="agent", text=_clip(out.get("reply") or "Noted.", 1500), ts=time.time()))
    return ch


# ---------------------------------------------------------------------------------------------- approve and document
def approve(s: Session, ids: list[str], reqs, by: str) -> dict:
    made, skipped = [], []
    now = time.time()
    for d in s.drafts:
        if d.id not in ids or d.status != "draft":
            continue
        if d.unverified and d.fit in ("Standard", "Configuration"):
            note = " (fit not backed by a DocType on the site)"
        else:
            note = ""
        r = reqs.add_gathered(module=s.module, text=d.text, process=d.process, fit=d.fit, priority=d.priority,
                              acceptance=d.acceptance, doctypes=d.doctypes, rationale=(d.rationale + (" Workaround: " + d.workaround if d.workaround else "") + note).strip(),
                              source="interview:" + s.id, by=by, ts=now)
        d.status, d.req_id = "approved", r.id
        made.append({"draft": d.id, "requirement": r.id, "status": r.status})
    open_q = [q.id for q in s.questions if q.status == "open" and any(b in ids for b in q.blocks)]
    return {"approved": made, "skipped": skipped, "open_questions_on_these": open_q}


def _mm(text: str, n: int = 46) -> str:
    t = re.sub(r"[^A-Za-z0-9 .,/&-]", " ", text)
    t = re.sub(r"\s+", " ", t).strip()
    return (t[:n] + "...") if len(t) > n else t


def brd_markdown(s: Session) -> str:
    live = [d for d in s.drafts if d.status != "rejected"]
    ok = [d for d in live if d.status == "approved"]
    use = ok or live
    lines = [f"# Business Requirement Document: {s.title}", "",
             f"Module: {s.module}  |  Requirements: {len(use)} ({'approved' if ok else 'draft, not yet approved'})  |  Generated {time.strftime('%d %b %Y', time.localtime())}", "",
             "## 1. Scope and approach", "",
             f"This document records the requirements gathered for {s.module}. Each requirement is judged against the ERPNext v15 "
             "functionality available on the development site: Standard (ERPNext does it), Configuration (settings, fields, workflow, permissions), "
             "Customization (script or new DocType) or Not possible.", ""]
    fits = {f: [d for d in use if d.fit == f] for f in FITS}
    lines += ["## 2. Fit summary", "", "| Fit | Count |", "|---|---|"] + [f"| {f} | {len(v)} |" for f, v in fits.items()] + [""]
    procs: dict = {}
    for d in use:
        procs.setdefault(d.process or "General", []).append(d)
    lines += ["## 3. Process flows", ""]
    for p, ds in procs.items():
        lines += [f"### {p}", "", "```mermaid", "flowchart LR"]
        for i, d in enumerate(ds):
            lines.append(f'  {d.id}["{d.id}: {_mm(d.text)}"]')
            if i:
                lines.append(f"  {ds[i - 1].id} --> {d.id}")
        lines += ["```", ""]
    lines += ["## 4. Requirements", ""]
    for p, ds in procs.items():
        lines += [f"### {p}", ""]
        for d in ds:
            lines += [f"**{d.id}** ({d.priority}, {d.fit}{', unverified' if d.unverified else ''}) {d.text}", ""]
            if d.doctypes:
                lines.append(f"- ERPNext: {', '.join(d.doctypes)}")
            if d.rationale:
                lines.append(f"- Why this fit: {d.rationale}")
            if d.workaround:
                lines.append(f"- Workaround: {d.workaround}")
            for a in d.acceptance:
                lines.append(f"- Acceptance: {a}")
            lines.append("")
    oq = [q for q in s.questions if q.status == "open"]
    lines += ["## 5. Open questions", ""]
    lines += [f"- **{q.id}** {q.text}" + (f" Suggested: {q.suggested}." if q.suggested else "") for q in oq] or ["None."]
    lines += ["", "## 6. Decisions taken", ""]
    lines += [f"- **{q.id}** {q.text} Answer: {q.answer}" for q in s.questions if q.status == "answered"] or ["None recorded."]
    return "\n".join(lines) + "\n"
