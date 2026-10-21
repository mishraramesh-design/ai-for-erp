"""Orchestration kernel: runs requirements through a governed task graph (Charter D1-D6).

For every group of related requirements the graph is
    triage (rules) -> analyze (analyst model) -> plan (developer model + validators) -> review (reviewer model)
    -> gate (policy, or a person for high risk) -> apply (engine, all-or-nothing) -> verify (engine reads the live site back)
Failures spawn a bounded repair chain with the failure as feedback; the last attempt escalates to the stronger model in the
role's chain. Everything is recorded in a per-run event log. The kernel never writes to ERPNext itself: only the Applier does,
one change set at a time, under a single write lock.
"""
from __future__ import annotations

import json
import re
import os
import threading
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from . import agent as agents
from .llm import LlmBudgetExceeded, LlmError
from .spec import ChangeSet, describe
from .triage import external_clauses, hints as req_hints, triage as rule_triage
from .verify import run_tests, verify_changeset

KINDS = ("triage", "analyze", "plan", "review", "gate", "apply", "verify")
TIER = {"triage": "rule", "analyze": "model", "plan": "model", "review": "model", "gate": "rule", "apply": "engine", "verify": "engine"}
CHAIN = ("plan", "review", "gate", "apply", "verify")
DONE = ("done", "skipped")


@dataclass
class Policy:
    batch_size: int = 8
    max_repairs: int = 2          # extra plan attempts after the first
    max_llm_calls: int = 80       # per run
    max_tokens_out: int = 300000  # per run: output tokens (hidden thinking is billed as output); the real cost driver
    workers: int = 3
    use_analyst: bool = True
    use_reviewer: bool = True
    use_tester: bool = True
    auto_low: bool = True
    auto_medium: bool = True      # high risk (permissions, workflows) is ALWAYS held for a person
    analyze_only: bool = False    # classify and stop: no plan, no change (cheap survey of a whole module)

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> "Policy":
        p = cls()
        for k, v in (d or {}).items():
            if hasattr(p, k) and isinstance(v, type(getattr(p, k))):
                setattr(p, k, v)
        return p


@dataclass
class Task:
    id: str
    kind: str
    group: str
    attempt: int = 0
    deps: list = field(default_factory=list)
    status: str = "pending"       # pending | running | done | failed | skipped | held
    note: str = ""
    result: dict = field(default_factory=dict)
    started: float = 0.0
    ended: float = 0.0

    @property
    def tier(self) -> str:
        if self.kind == "gate" and (self.status == "held" or self.result.get("decision") == "human"):
            return "human"
        return TIER[self.kind]

    def public(self) -> dict:
        return {"id": self.id, "kind": self.kind, "group": self.group, "attempt": self.attempt, "deps": self.deps,
                "status": self.status, "tier": self.tier, "note": self.note, "result": self.result,
                "ms": int((self.ended - self.started) * 1000) if self.ended and self.started else 0}


@dataclass
class Outcome:
    status: str = "done"
    note: str = ""
    result: dict = field(default_factory=dict)
    spawn: list = field(default_factory=list)


class Run:
    def __init__(self, id: str, project: str, title: str, policy: Policy):
        self.id, self.project, self.title, self.policy = id, project, title, policy
        self.status = "running"   # running | waiting_approval | done | failed | cancelled
        self.created = time.time()
        self.ended = 0.0
        self.tasks: list[Task] = []
        self.groups: dict[str, dict] = {}
        self.events: list[dict] = []
        self.error = ""

    def task(self, tid: str) -> Optional[Task]:
        return next((t for t in self.tasks if t.id == tid), None)

    def to_dict(self) -> dict:
        return {"id": self.id, "project": self.project, "title": self.title, "policy": asdict(self.policy), "status": self.status,
                "created": self.created, "ended": self.ended, "error": self.error, "groups": self.groups,
                "tasks": [asdict(t) for t in self.tasks]}

    @classmethod
    def from_dict(cls, d: dict, events: list) -> "Run":
        r = cls(d["id"], d["project"], d["title"], Policy.from_dict(d.get("policy")))
        r.status, r.created, r.ended, r.error, r.groups = d["status"], d["created"], d.get("ended", 0.0), d.get("error", ""), d["groups"]
        r.tasks = [Task(**t) for t in d["tasks"]]
        r.events = events
        return r


class RunStore:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def save(self, run: Run) -> None:
        with self._lock:
            p = self.root / f"{run.id}.json"
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps(run.to_dict()), encoding="utf-8")
            os.replace(tmp, p)

    def append_event(self, run_id: str, ev: dict) -> None:
        with self._lock, (self.root / f"{run_id}.events.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(ev) + "\n")

    def load(self, run_id: str) -> Optional[Run]:
        p = self.root / f"{run_id}.json"
        if not p.exists() or not run_id.isalnum():
            return None
        ev: list = []
        ep = self.root / f"{run_id}.events.jsonl"
        if ep.exists():
            for ln in ep.read_text(encoding="utf-8").splitlines():
                try:
                    ev.append(json.loads(ln))
                except Exception:
                    continue
        return Run.from_dict(json.loads(p.read_text(encoding="utf-8")), ev)

    def list_ids(self) -> list:
        return sorted((f.stem for f in self.root.glob("*.json") if f.suffix == ".json" and "." not in f.stem), reverse=True)


@dataclass
class Deps:
    """Everything a run needs from the outside. Built per run so tests can inject fakes."""
    reqs: Any
    ledger: Any
    erp: Any
    snap: Any
    applier: Any
    llm_for: Callable        # (role, run_id) -> object with complete_json / escalated / configured
    llm_calls: Callable      # (run_id) -> int
    roles_enabled: Callable  # (role) -> bool
    llm_tokens_out: Optional[Callable] = None  # (run_id) -> int


class Kernel:
    def __init__(self, store: RunStore, deps_factory: Callable[[], Deps]):
        self.store, self.deps_factory = store, deps_factory
        self._runs: dict[str, Run] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._cv = threading.Condition()
        self._write_lock = threading.Lock()
        self._cancel: set = set()
        self._ev_lock = threading.Lock()

    # ------------------------------------------------------------------ public
    def start(self, project: str, ids: list, policy: Optional[dict] = None, guidance: str = "", title: str = "") -> Run:
        deps = self.deps_factory()
        pol = Policy.from_dict(policy)
        def stale_plan(r) -> bool:  # planned by a cancelled/abandoned run, never applied
            if r.status != "planned":
                return False
            if any(x.status in ("queued", "running", "waiting_approval") and r.id in sum((g["ids"] for g in x.groups.values()), [])
                   for x in self._runs.values()):
                return False
            return not any((deps.ledger.get(c) is not None and deps.ledger.get(c).status in ("applied", "applying")) for c in r.change_sets)
        reqs = [r for r in (deps.reqs.get(i) for i in ids) if r and (r.status in ("backlog", "blocked", "needs_input") or stale_plan(r))]
        if not reqs:
            raise ValueError("None of these requirements is waiting to be built (they may already be planned, built or verified).")
        run = Run(uuid.uuid4().hex[:12], project, title or f"Build {len(reqs)} requirement(s)", pol)
        buckets: dict = {}
        for r in reqs:
            buckets.setdefault((r.module, r.branch, r.process), []).append(r)
        n = 0
        for key, rs in buckets.items():
            for i in range(0, len(rs), pol.batch_size):
                chunk = rs[i:i + pol.batch_size]
                gid = f"g{n + 1}"
                n += 1
                label = " / ".join(x for x in (key[1], key[2]) if x) or key[0]
                run.groups[gid] = {"label": label, "ids": [r.id for r in chunk], "all_ids": [r.id for r in chunk], "guidance": guidance, "feedback": "", "hints": {},
                                   "approach": {}, "cs_id": "", "attempt": 0, "closed": False, "outcome": ""}
                run.tasks.append(Task(f"{gid}.triage", "triage", gid))
                run.tasks.append(Task(f"{gid}.analyze", "analyze", gid, deps=[f"{gid}.triage"]))
                if not pol.analyze_only:
                    run.tasks.extend(self._chain(gid, 0, [f"{gid}.analyze"]))
        self._runs[run.id] = run
        self._log(run, "-", "info", f"Run started: {len(reqs)} requirement(s) in {n} group(s).")
        self._launch(run)
        return run

    def get(self, run_id: str) -> Optional[Run]:
        return self._runs.get(run_id) or self._load(run_id)

    def approve(self, run_id: str, task_id: str, by: str) -> Run:
        run = self._need(run_id)
        t = self._held(run, task_id)
        t.status, t.note = "pending", f"approved by {by}"
        t.result = {**t.result, "approved_by": by}
        run.groups[t.group]["approved"], run.groups[t.group]["approved_cs"] = by, run.groups[t.group]["cs_id"]
        self._log(run, task_id, "ok", f"Approved by {by}.", tier="human")
        self._launch(run)
        return run

    def reject(self, run_id: str, task_id: str, by: str, reason: str = "") -> Run:
        run = self._need(run_id)
        t = self._held(run, task_id)
        d = self._make_deps()
        g = run.groups[t.group]
        t.status, t.note, t.ended = "failed", f"rejected by {by}" + (f": {reason}" if reason else ""), time.time()
        cs = d.ledger.get(g["cs_id"]) if g["cs_id"] else None
        if cs:
            cs.status = "discarded"
            d.ledger.save(cs)
        d.reqs.mark(g["ids"], "backlog", g["cs_id"], note=f"Plan rejected by {by}" + (f": {reason}" if reason else ""))
        self._close(run, t.group, "rejected", skip_after=t)
        self._log(run, task_id, "warn", t.note, tier="human")
        self._launch(run)
        return run

    def cancel(self, run_id: str) -> Run:
        run = self._need(run_id)
        self._cancel.add(run_id)
        with self._cv:
            self._cv.notify_all()
        if not self._alive(run_id):
            self._finish(run, "cancelled")
        return run

    def undo(self, cs_id: str, by: str) -> dict:
        d = self._make_deps()
        cs = d.ledger.get(cs_id)
        if not cs or cs.status != "applied":
            raise ValueError("Only an applied change set can be undone.")
        with self._write_lock:
            problems = d.applier.undo(cs)
        cs.status = "undone"
        d.ledger.save(cs)
        d.reqs.mark(cs.requirement_ids, "backlog", cs_id, note=f"Change undone by {by}")
        return {"ok": not problems, "problems": problems}

    # ------------------------------------------------------------------ plumbing
    def _make_deps(self) -> Deps:
        return self.deps_factory()

    def _need(self, run_id: str) -> Run:
        r = self.get(run_id)
        if not r:
            raise KeyError("unknown run")
        return r

    def _load(self, run_id: str) -> Optional[Run]:
        r = self.store.load(run_id)
        if r:
            self._runs[run_id] = r
        return r

    def _alive(self, run_id: str) -> bool:
        t = self._threads.get(run_id)
        return bool(t and t.is_alive())

    def _held(self, run: Run, task_id: str) -> Task:
        t = run.task(task_id)
        if not t or t.kind != "gate" or t.status != "held":
            raise ValueError("That step is not waiting for approval.")
        return t

    def _launch(self, run: Run) -> None:
        if run.status in ("done", "failed", "cancelled"):
            return
        run.status = "running"
        with self._cv:
            self._cv.notify_all()
        if not self._alive(run.id):
            th = threading.Thread(target=self._loop, args=(run,), name=f"run-{run.id}", daemon=True)
            self._threads[run.id] = th
            th.start()

    def _chain(self, gid: str, attempt: int, after: list) -> list:
        tasks, prev = [], after
        for k in CHAIN:
            t = Task(f"{gid}.{k}.{attempt}", k, gid, attempt, deps=list(prev))
            tasks.append(t)
            prev = [t.id]
        return tasks

    def _log(self, run: Run, task: str, level: str, text: str, tier: str = "") -> None:
        with self._ev_lock:
            ev = {"n": len(run.events), "ts": time.time(), "task": task, "level": level, "text": text, "tier": tier}
            run.events.append(ev)
            try:
                self.store.append_event(run.id, ev)
            except OSError:
                pass

    def _persist(self, run: Run) -> None:
        try:
            self.store.save(run)
        except OSError:
            pass

    def _finish(self, run: Run, status: str, error: str = "") -> None:
        run.status, run.ended, run.error = status, time.time(), error
        self._log(run, "-", "ok" if status == "done" else "warn", f"Run {status}." + (f" {error}" if error else ""))
        self._persist(run)

    # ------------------------------------------------------------------ scheduler
    def _loop(self, run: Run) -> None:
        deps = self._make_deps()
        pool = ThreadPoolExecutor(max_workers=max(1, run.policy.workers))
        futures: dict = {}
        try:
            for t in run.tasks:  # resume after a restart: whatever was mid-flight starts again
                if t.status == "running":
                    t.status = "pending"
            while True:
                if run.id in self._cancel:
                    for t in run.tasks:
                        if t.status in ("pending", "held"):
                            t.status = "skipped"
                    wait(list(futures), timeout=60)
                    self._finish(run, "cancelled")
                    return
                by_id = {t.id: t for t in run.tasks}
                ready = [t for t in run.tasks if t.status == "pending" and all(by_id[d].status in DONE for d in t.deps)]
                dead = [t for t in run.tasks if t.status == "pending" and any(by_id[d].status == "failed" for d in t.deps)]
                for t in dead:
                    t.status, t.note = "skipped", "an earlier step failed"
                for t in ready:
                    t.status, t.started = "running", time.time()
                    futures[t.id] = pool.submit(self._exec, run, t, deps)
                if futures:
                    fin, _ = wait(list(futures.values()), timeout=0.5, return_when=FIRST_COMPLETED)
                    for tid in [k for k, f in futures.items() if f in fin]:
                        self._complete(run, run.task(tid), futures.pop(tid))
                    self._persist(run)
                    continue
                if dead:
                    continue
                if any(t.status == "held" for t in run.tasks):
                    run.status = "waiting_approval"
                    self._persist(run)
                    with self._cv:
                        self._cv.wait(timeout=2.0)
                    if run.id not in self._cancel and run.status == "waiting_approval" and not any(t.status == "pending" for t in run.tasks):
                        continue
                    run.status = "running"
                    continue
                stuck = [t for t in run.tasks if t.status == "pending"]
                if not stuck:
                    break
                for t in stuck:  # unreachable: depends on something that can never finish
                    t.status, t.note = "skipped", "unreachable"
            failed = [t for t in run.tasks if t.status == "failed"]
            self._finish(run, "done")
            if failed:
                self._log(run, "-", "warn", f"{len(failed)} step(s) did not complete; see the group outcomes.")
        except Exception as e:  # noqa: BLE001 - a kernel bug must end the run visibly, not hang it
            self._finish(run, "failed", f"{type(e).__name__}: {e}")
        finally:
            pool.shutdown(wait=False)

    def _complete(self, run: Run, t: Task, fut) -> None:
        try:
            out: Outcome = fut.result()
        except LlmBudgetExceeded as e:
            out = Outcome("failed", f"Budget reached: {e}")
        except LlmError as e:
            out = Outcome("failed", f"Model error: {e}")
        except Exception as e:  # noqa: BLE001
            out = Outcome("failed", f"{type(e).__name__}: {e}")
        t.status, t.note, t.ended = out.status, out.note or t.note, time.time()
        t.result = {**t.result, **out.result}
        if out.status == "failed":
            self._log(run, t.id, "err", t.note)
            self._abandon_group_if_stuck(run, t)
        for n in out.spawn:
            run.tasks.append(n)

    def _abandon_group_if_stuck(self, run: Run, t: Task) -> None:
        g = run.groups[t.group]
        if not g["closed"] and t.kind in ("triage", "analyze", "plan"):
            d = self._make_deps()
            d.reqs.mark(g["ids"], "blocked", note=t.note[:300])
            self._close(run, t.group, "failed")

    def _close(self, run: Run, gid: str, outcome: str, skip_after: Optional[Task] = None) -> None:
        g = run.groups[gid]
        g["closed"], g["outcome"] = True, outcome
        for x in run.tasks:
            if x.group == gid and x.status == "pending" and x is not skip_after:
                x.status, x.note = "skipped", f"group {outcome}"

    # ------------------------------------------------------------------ executors
    def _exec(self, run: Run, t: Task, deps: Deps) -> Outcome:
        g = run.groups[t.group]
        if g["closed"]:
            return Outcome("skipped", f"group {g['outcome']}")
        if run.id in self._cancel:
            return Outcome("skipped", "cancelled")
        if t.kind in ("analyze", "plan", "review") and deps.llm_calls(run.id) >= run.policy.max_llm_calls:
            raise LlmBudgetExceeded(f"this run's limit of {run.policy.max_llm_calls} model calls is used up")
        if t.kind in ("analyze", "plan", "review") and deps.llm_tokens_out and deps.llm_tokens_out(run.id) >= run.policy.max_tokens_out:
            raise LlmBudgetExceeded(f"this run's limit of {run.policy.max_tokens_out} output tokens is used up (cost guard)")
        return getattr(self, f"_x_{t.kind}")(run, t, g, deps)

    def _reqs(self, g: dict, deps: Deps) -> list:
        return [r for r in (deps.reqs.get(i) for i in g["ids"]) if r]

    # -- triage: rules only
    def _x_triage(self, run, t, g, d: Deps) -> Outcome:
        reqs, out_n = self._reqs(g, d), 0
        keep = []
        for r in reqs:
            verdict, why = rule_triage(r.text)
            if verdict == "out_of_scope":
                d.reqs.mark([r.id], "blocked", note=why)
                self._log(run, t.id, "warn", f"[{r.id}] {why}", tier="rule")
                out_n += 1
            else:
                keep.append(r)
                g["hints"][str(r.id)] = ", ".join(req_hints(r.text) + external_clauses(r.text)[:1])
        g["ids"] = [r.id for r in keep]
        if not keep:
            self._close(run, t.group, "nothing to build")
            return Outcome("done", f"all {out_n} out of scope", {"out_of_scope": out_n})
        self._log(run, t.id, "ok", f"{len(keep)} candidate(s), {out_n} set aside by rules, 0 model calls.", tier="rule")
        return Outcome("done", f"{len(keep)} candidates", {"candidates": len(keep), "out_of_scope": out_n})

    # -- analyze: cheap model decides configure / customize / standard / out of scope / clarify
    def _x_analyze(self, run, t, g, d: Deps) -> Outcome:
        reqs = self._reqs(g, d)
        llm = d.llm_for("analyst", run.id)
        if not run.policy.use_analyst or not d.roles_enabled("analyst"):
            return Outcome("skipped", "analyst not used")
        res = agents.analyst(reqs, d.snap, llm)
        keep, counts = [], {}
        for r in reqs:
            a = res.get(r.id, {"class": "configure", "reason": "", "approach": "", "question": ""})
            counts[a["class"]] = counts.get(a["class"], 0) + 1
            if a["class"] == "standard":
                d.reqs.mark([r.id], "standard", note=a["approach"] or a["reason"])
            elif a["class"] == "out_of_scope":
                d.reqs.mark([r.id], "blocked", note=a["reason"])
            elif a["class"] == "clarify":
                d.reqs.mark([r.id], "needs_input", note=a["question"] or a["reason"])
            else:
                keep.append(r)
                g["approach"][str(r.id)] = a["approach"]
                if run.policy.analyze_only:
                    d.reqs.mark([r.id], "backlog", note=f"Analysed ({a['class']}): {a['approach'] or a['reason']}"[:300])
        g["ids"] = [r.id for r in keep]
        if run.policy.analyze_only:
            self._log(run, t.id, "ok", "Analyst: " + ", ".join(f"{v} {k}" for k, v in sorted(counts.items())) + " (analysis only, nothing built)", tier="model")
            self._close(run, t.group, "analysed")
            return Outcome("done", f"{len(keep)} to build", {"classes": counts})
        self._log(run, t.id, "ok", "Analyst: " + ", ".join(f"{v} {k}" for k, v in sorted(counts.items())), tier="model")
        if not keep:
            self._close(run, t.group, "nothing to build")
        return Outcome("done", f"{len(keep)} to build", {"classes": counts})

    # -- plan: developer model returns typed ops; validators + auto-repair run inside agent._run
    def _x_plan(self, run, t, g, d: Deps) -> Outcome:
        reqs = self._reqs(g, d)
        llm = d.llm_for("developer", run.id)
        if t.attempt >= run.policy.max_repairs and hasattr(llm, "escalated"):
            llm = llm.escalated()
        hints = {int(k): (g["approach"].get(k) or v) for k, v in g["hints"].items() if g["approach"].get(k) or v}
        res = agents.plan_batch(reqs, d.snap, llm, guidance=g.get("guidance", ""), created_by=f"run:{run.id}",
                                session_id=run.id, hints=hints, feedback=g.get("feedback", ""))
        cs = res.changeset
        if not cs or not cs.ops:
            reasons = {n.get("id"): n.get("reason", "") for n in (cs.not_covered if cs else [])}
            if res.error and not cs:  # the model was unreachable or unusable: not the requirement's fault, keep it open
                d.reqs.mark(g["ids"], "backlog", note=f"Model error, retry: {res.error}"[:300])
                self._log(run, t.id, "warn", res.reply, tier="model")
                self._close(run, t.group, "model error")
                return Outcome("failed", f"model error: {res.error}"[:200], {})
            d.reqs.mark(g["ids"], "needs_input", note=(res.reply or "The model proposed nothing buildable.")[:300])
            self._log(run, t.id, "warn", res.reply or "No buildable operations.", tier="model")
            self._close(run, t.group, "nothing to build")
            return Outcome("done", "nothing buildable", {"not_covered": reasons})
        errors = [i for i in cs.issues if i["level"] == "error"]
        d.ledger.save(cs)
        g["cs_id"], g["attempt"] = cs.id, t.attempt
        g.pop("approved", None)
        if errors:
            msg = "; ".join(i["message"] for i in errors)[:400]
            self._log(run, t.id, "err", f"Plan failed validation after {res.llm_calls} model call(s): {msg}", tier="engine")
            return self._repair(run, t, g, f"Validation errors: {msg}", "plan could not pass the validators")
        warn = [i for i in cs.issues if i["level"] == "warning"]
        d.reqs.mark(cs.requirement_ids, "planned", cs.id, note="")
        self._log(run, t.id, "ok", f"Plan: {len(cs.ops)} operation(s), risk {cs.risk()}, {res.llm_calls} model call(s)"
                  + (f", {len(warn)} warning(s)" if warn else "") + ("; escalated to the stronger model" if res.escalated else ""), tier="model")
        return Outcome("done", f"{len(cs.ops)} ops, risk {cs.risk()}",
                       {"cs_id": cs.id, "risk": cs.risk(), "ops": len(cs.ops), "lines": [describe(o) for o in cs.ops][:40],
                        "not_covered": cs.not_covered, "assumptions": cs.assumptions, "warnings": [i["message"] for i in warn]})

    # -- review: a second model checks the claims; failures feed one repair
    def _x_review(self, run, t, g, d: Deps) -> Outcome:
        cs = d.ledger.get(g["cs_id"])
        if not run.policy.use_reviewer or not d.roles_enabled("reviewer") or not cs or not cs.requirement_ids:
            return Outcome("skipped", "reviewer not used")
        verdicts = agents.reviewer(self._reqs(g, d), cs, d.llm_for("reviewer", run.id))
        bad = {i: v for i, v in verdicts.items() if v["verdict"] != "pass"}
        self._log(run, t.id, "ok" if not bad else "warn",
                  f"Reviewer: {len(verdicts) - len(bad)} pass, {len(bad)} need work.", tier="model")
        for i, v in bad.items():
            self._log(run, t.id, "warn", f"[{i}] {v['verdict']}: {v['issue']}", tier="model")
        res = {"verdicts": {str(i): v for i, v in verdicts.items()}}
        hard = {i: v for i, v in bad.items() if v["verdict"] == "fail"}  # "partial" is kept and flagged, not re-planned
        if hard and t.attempt < run.policy.max_repairs:
            fb = "\n".join(f"- requirement {i}: {v['verdict']}: {v['issue']}" for i, v in bad.items())
            cs.status = "discarded"
            d.ledger.save(cs)
            out = self._repair(run, t, g, "The reviewer found gaps:\n" + fb, "reviewer found gaps")
            out.result = {**res, **out.result}
            return out
        if bad:  # out of repairs: build what passed, hand the rest back with the reviewer's reason
            for i, v in bad.items():
                if v["verdict"] == "partial":  # keep it: built and traceable, but never called verified
                    g.setdefault("partial", {})[str(i)] = v["issue"][:300]
                else:
                    d.reqs.mark([i], "needs_input", note=f"Reviewer: {v['issue']}")
            cs.requirement_ids = [i for i in cs.requirement_ids if i not in bad or bad[i]["verdict"] == "partial"]
            d.ledger.save(cs)
        return Outcome("done", f"{len(bad)} unresolved" if bad else "all claims pass", res)

    # -- gate: policy decides; high risk waits for a person
    def _x_gate(self, run, t, g, d: Deps) -> Outcome:
        cs = d.ledger.get(g["cs_id"])
        if not cs:
            return Outcome("failed", "change set missing")
        risk, pol = cs.risk(), run.policy
        if g.get("approved") and g.get("approved_cs") == g["cs_id"]:
            return Outcome("done", f"approved by {g['approved']}", {"decision": "human", "risk": risk})
        auto = (risk == "low" and pol.auto_low) or (risk == "medium" and pol.auto_medium)
        if auto:
            self._log(run, t.id, "ok", f"Risk {risk}: approved by policy.", tier="rule")
            return Outcome("done", f"auto (risk {risk})", {"decision": "policy", "risk": risk})
        self._log(run, t.id, "warn", f"Risk {risk}: waiting for a person to approve.", tier="human")
        return Outcome("held", f"needs approval (risk {risk})", {"decision": "waiting", "risk": risk})

    # -- apply: the only writer; all-or-nothing
    def _x_apply(self, run, t, g, d: Deps) -> Outcome:
        cs = d.ledger.get(g["cs_id"])
        cs.status = "applying"
        d.ledger.save(cs)
        with self._write_lock:
            ok, steps, err = d.applier.apply(cs)
        cs.steps = steps
        if not ok:
            cs.status, cs.error = "failed", err
            d.ledger.save(cs)
            self._log(run, t.id, "err", err, tier="engine")
            return self._repair(run, t, g, f"Applying failed (and was rolled back): {err}", "apply failed and was rolled back")
        cs.status = "applied"
        d.ledger.save(cs)
        d.reqs.mark(cs.requirement_ids, "built", cs.id)
        self._log(run, t.id, "ok", f"Applied {len(steps)} step(s) to the development site.", tier="engine")
        return Outcome("done", f"{len(steps)} steps applied", {"steps": len(steps)})

    # -- verify: read the live site back, run behavioural tests, then mark requirements verified
    def _x_verify(self, run, t, g, d: Deps) -> Outcome:
        cs = d.ledger.get(g["cs_id"])
        checks = verify_changeset(cs, d.erp)
        failed = [c for c in checks if c.status == "fail"]
        tests: list = []
        if not failed and run.policy.use_tester and d.roles_enabled("tester") and cs.requirement_ids:
            try:
                spec = agents.tester(self._reqs(g, d), cs, d.snap, d.llm_for("tester", run.id))
                with self._write_lock:
                    own = {getattr(o, "fieldname", None) for o in cs.ops if getattr(o, "fieldname", None)}
                    own |= {f.fieldname for o in cs.ops for f in (getattr(o, "fields", None) or [])}
                    terms = {re.sub(r"[^a-z0-9]+", " ", str(x).lower()).strip() for o in cs.ops for x in (
                        getattr(o, "fieldname", None), getattr(o, "label", None), getattr(o, "name", None), getattr(o, "role_name", None)) if x}
                    terms |= {re.sub(r"[^a-z0-9]+", " ", f.label.lower()).strip() for o in cs.ops for f in (getattr(o, "fields", None) or [])}
                    tests = run_tests(spec, d.erp, own_fields=own, own_terms={t for t in terms if len(t) > 2})
            except LlmError as e:
                self._log(run, t.id, "warn", f"Tester unavailable: {e}", tier="model")
        failed_tests = [x for x in tests if x["status"] == "fail"]
        evidence = [c.to_dict() for c in checks] + [{"test": x} for x in tests]
        res = {"checks": len(checks), "passed": len(checks) - len(failed), "tests": len(tests),
               "tests_failed": len(failed_tests), "evidence": evidence[:60]}
        if failed or failed_tests:
            detail = "; ".join(f"{c.name}: {c.detail}" for c in failed[:5]) + "; ".join(f"{x['description']}: {x['detail']}" for x in failed_tests[:5])
            self._log(run, t.id, "err", f"Verification failed: {detail[:400]}", tier="engine")
            with self._write_lock:
                problems = d.applier.undo(cs)
            cs.status = "undone"
            d.ledger.save(cs)
            d.reqs.mark(cs.requirement_ids, "backlog", cs.id, note="Verification failed; change undone")
            out = self._repair(run, t, g, f"Verification on the live site failed (change was undone): {detail[:600]}", "verification failed; change undone")
            out.result = {**res, "undone": not problems, **out.result}
            return out
        inconclusive = any(c.status == "inconclusive" for c in checks) or any(x["status"] == "inconclusive" for x in tests)
        untested = any(o.op == "add_server_validation" for o in cs.ops) and not any(x["status"] == "pass" for x in tests)
        status = "built" if (inconclusive or untested) else "verified"
        part = {int(k): v for k, v in (g.get("partial") or {}).items()}
        full = [i for i in cs.requirement_ids if i not in part]
        d.reqs.mark(full, status, cs.id, evidence=evidence[:60], by=f"run:{run.id}",
                    note="Applied, but no behavioural test passed for its validation rule: needs a person to check." if status == "built"
                    else "Verified on the live site.")
        for i, why in part.items():  # reviewer said "partial": applied and checked, but a person decides if it is enough
            d.reqs.mark([i], "built", cs.id, evidence=evidence[:60], by=f"run:{run.id}", note=f"Partly covered: {why}")
        for n in cs.not_covered:
            try:
                rid = int(n["id"])
            except (KeyError, TypeError, ValueError):
                continue
            why = str(n.get("reason", ""))[:300]
            if rid in cs.requirement_ids:  # something was applied for it: keep that credit, flag what is still open
                d.reqs.mark([rid], "built", cs.id, evidence=evidence[:60], by=f"run:{run.id}", note=f"Partly covered; still open: {why}")
            else:
                d.reqs.mark([rid], "needs_input", note=why)
        self._log(run, t.id, "ok", f"Verified on the live site: {res['passed']}/{res['checks']} checks, {len(tests)} behavioural test(s)"
                  + (" (some inconclusive: left as built)" if inconclusive else " (a validation rule has no passing behavioural test: left as built)" if untested else "") + ".", tier="engine")
        self._close(run, t.group, status)
        return Outcome("done", f"{res['passed']}/{res['checks']} checks pass", res)

    # -- repair: bounded retry with the failure as feedback; the last attempt escalates the model
    def _repair(self, run: Run, t: Task, g: dict, feedback: str, why: str) -> Outcome:
        if run.id in self._cancel:  # a cancelled run must not touch requirement statuses after the fact
            return Outcome("skipped", "cancelled")
        d = self._make_deps()
        ids = list(g["ids"])
        same = bool(feedback) and g.get("feedback", "")[:300] == feedback[:300]
        if (t.attempt >= run.policy.max_repairs or same) and len(ids) > 1 and not g.get("split"):
            # divide and conquer: one stubborn requirement must not sink its neighbours
            spawn = []
            for rid in ids:
                sid = f"{t.group}s{rid}"
                run.groups[sid] = {"label": f"{g['label']} · {rid}", "ids": [rid], "all_ids": [rid], "guidance": g.get("guidance", ""),
                                   "feedback": "", "hints": {k: v for k, v in g["hints"].items() if k == str(rid)},
                                   "approach": {k: v for k, v in g["approach"].items() if k == str(rid)},
                                   "cs_id": "", "attempt": 0, "closed": False, "outcome": "", "split": True}
                spawn.extend(self._chain(sid, 0, [t.id]))
            d.reqs.mark(ids, "backlog", note=f"Splitting into single requirements: {why}")
            self._close(run, t.group, f"split into {len(ids)}", skip_after=t)
            self._log(run, t.id, "warn", f"{why}; retrying each of the {len(ids)} requirements on its own.", tier="rule")
            return Outcome("done", f"split into {len(ids)} groups", {"split": len(ids)}, spawn=spawn)
        if t.attempt >= run.policy.max_repairs:
            d.reqs.mark(ids, "blocked", note=f"{why} after {t.attempt + 1} attempt(s): {feedback[:240]}")
            self._close(run, t.group, "blocked", skip_after=t)
            self._log(run, t.id, "err", f"Giving up after {t.attempt + 1} attempt(s): {why}.")
            return Outcome("failed", f"{why}; out of retries")
        if same:  # the same error twice: another try would only repeat it
            d.reqs.mark(ids, "blocked", note=f"{why}; stuck on the same error twice: {feedback[:240]}")
            self._close(run, t.group, "blocked", skip_after=t)
            self._log(run, t.id, "err", f"Stopping: the same error came back ({why}). Not spending more model calls on it.")
            return Outcome("failed", f"{why}; same error twice")
        g["feedback"] = feedback
        nxt = t.attempt + 1
        for x in run.tasks:
            if x.group == t.group and x.status == "pending" and x.attempt == t.attempt and x.kind in CHAIN:
                x.status, x.note = "skipped", "replaced by a repair attempt"
        d.reqs.mark(ids, "backlog", note=f"Repairing: {why}")
        self._log(run, t.id, "warn", f"Repair attempt {nxt}: {why}.", tier="rule")
        spawn = self._chain(t.group, nxt, [t.id])
        return Outcome("done", f"repair attempt {nxt} queued", {"repair": nxt}, spawn=spawn)

    # ------------------------------------------------------------------ views
    def public(self, run: Run, since: int = 0) -> dict:
        tiers = {"rule": 0, "engine": 0, "model": 0, "human": 0}
        for ev in run.events:
            if ev["tier"] in tiers:
                tiers[ev["tier"]] += 1
        d = {"id": run.id, "title": run.title, "status": run.status, "created": run.created, "ended": run.ended,
             "error": run.error, "policy": asdict(run.policy), "tiers": tiers,
             "tasks": [t.public() for t in run.tasks], "events": run.events[since:], "next": len(run.events),
             "groups": {k: {kk: v for kk, v in g.items() if kk in ("label", "ids", "all_ids", "cs_id", "outcome", "closed", "attempt", "partial")} for k, g in list(run.groups.items())},
             "pending_approvals": [t.id for t in run.tasks if t.status == "held"]}
        return d
