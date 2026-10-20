"""Model configuration, role routing, fallback/escalation and cost accounting (R7).

Agents never know which model they get. They ask for a ROLE; this module maps the role to a provider and model,
fails over on errors, escalates to a stronger model when asked, counts tokens and money, and enforces budgets.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from pydantic import BaseModel, Field

from .llm import LlmBudgetExceeded, LlmError, call_provider

ROLES = {
    "analyst": "Triages each requirement: configure, customize, standard, out of scope, or needs a decision.",
    "developer": "Turns requirements into a typed change specification (the only thing that builds).",
    "reviewer": "Independent check that the specification really satisfies each requirement it claims.",
    "tester": "Writes behavioural test cases that the engine runs against the live site.",
    "chat": "Answers free-form requests on the page you have open.",
}


class Provider(BaseModel):
    id: str
    name: str = ""
    kind: str = "openai"  # openai (any OpenAI-compatible endpoint) | anthropic
    base_url: str = ""
    api_key: str = ""


class ModelRef(BaseModel):
    provider: str
    model: str

    def key(self) -> str:
        return f"{self.provider}/{self.model}"


class RoleCfg(BaseModel):
    enabled: bool = True
    primary: Optional[ModelRef] = None
    fallbacks: list[ModelRef] = Field(default_factory=list)  # tried in order on error, or on escalation
    max_tokens: int = 6000


class Price(BaseModel):
    in_per_m: float = 0.0  # USD per million input tokens
    out_per_m: float = 0.0


class ModelConfig(BaseModel):
    providers: list[Provider] = Field(default_factory=list)
    roles: dict[str, RoleCfg] = Field(default_factory=lambda: {r: RoleCfg() for r in ROLES})
    prices: dict[str, Price] = Field(default_factory=dict)  # key "provider/model"
    budget_usd: Optional[float] = None
    budget_calls: int = 5000
    timeout: float = 120.0

    def provider(self, pid: str) -> Optional[Provider]:
        return next((p for p in self.providers if p.id == pid), None)


def mask(key: str) -> str:
    return (key[:3] + "…" + key[-4:]) if len(key) > 10 else ("set" if key else "")


def apply_profile(cfg: ModelConfig, name: str, provider: str, cheap: str, strong: str) -> ModelConfig:
    """Presets. 'cheap' and 'strong' are model ids on one provider; the architect can still edit every role."""
    c, s = ModelRef(provider=provider, model=cheap), ModelRef(provider=provider, model=strong)
    R = RoleCfg
    presets = {
        "economy": {"analyst": R(primary=c), "developer": R(primary=c, fallbacks=[s]), "reviewer": R(enabled=False, primary=c),
                    "tester": R(enabled=False, primary=c), "chat": R(primary=c, fallbacks=[s])},
        "balanced": {"analyst": R(primary=c), "developer": R(primary=c, fallbacks=[s]), "reviewer": R(primary=c, fallbacks=[s]),
                     "tester": R(primary=c), "chat": R(primary=c, fallbacks=[s])},
        "quality": {"analyst": R(primary=s), "developer": R(primary=s), "reviewer": R(primary=s),
                    "tester": R(primary=c, fallbacks=[s]), "chat": R(primary=s)},
    }
    if name not in presets:
        raise ValueError("unknown profile")
    cfg.roles = {**cfg.roles, **presets[name]}
    return cfg


class ConfigStore:
    def __init__(self, path, env: Optional[dict] = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._cfg = ModelConfig()
        if self.path.exists():
            try:
                self._cfg = ModelConfig.model_validate_json(self.path.read_text(encoding="utf-8"))
            except Exception:
                pass
        elif env and env.get("STUDIO_LLM_API_KEY"):  # first boot: seed one provider from the environment
            m = env.get("STUDIO_LLM_MODEL", "gpt-4o-mini")
            self._cfg.providers.append(Provider(id="openai", name="OpenAI", kind="openai", api_key=env["STUDIO_LLM_API_KEY"],
                                                base_url=env.get("STUDIO_LLM_BASE_URL", "https://api.openai.com/v1")))
            for r in ROLES:
                self._cfg.roles[r] = RoleCfg(primary=ModelRef(provider="openai", model=m))
            self._save()

    def get(self) -> ModelConfig:
        return self._cfg

    def _save(self) -> None:
        self.path.write_text(self._cfg.model_dump_json(indent=2), encoding="utf-8")
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def public(self) -> dict:
        d = self._cfg.model_dump(mode="json")
        for p in d["providers"]:
            p["api_key"] = mask(p["api_key"])
        return d

    def update(self, data: dict) -> dict:
        """Replace the configuration. A masked or empty api_key keeps the stored one."""
        with self._lock:
            old_keys = {p.id: p.api_key for p in self._cfg.providers}
            new = ModelConfig.model_validate(data)
            for p in new.providers:
                if not p.api_key or "…" in p.api_key or p.api_key == "set":
                    p.api_key = old_keys.get(p.id, "")
            self._cfg = new
            self._save()
        return self.public()


class UsageLog:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.rows: list[dict] = []
        if self.path.exists():
            for ln in self.path.read_text(encoding="utf-8").splitlines():
                try:
                    self.rows.append(json.loads(ln))
                except Exception:
                    continue

    def add(self, row: dict) -> None:
        with self._lock:
            self.rows.append(row)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row) + "\n")

    def summary(self, project: str = "", run_id: str = "") -> dict:
        rows = [r for r in self.rows if (not project or r.get("project") == project) and (not run_id or r.get("run") == run_id)]
        by_role: dict = {}
        by_model: dict = {}
        total = {"calls": 0, "tokens_in": 0, "tokens_out": 0, "cost": 0.0, "unpriced_calls": 0, "failures": 0}
        for r in rows:
            for bucket, k in ((by_role, r["role"]), (by_model, r["model"])):
                b = bucket.setdefault(k, {"calls": 0, "tokens_in": 0, "tokens_out": 0, "cost": 0.0})
                b["calls"] += 1
                b["tokens_in"] += r["in"]
                b["tokens_out"] += r["out"]
                b["cost"] += r["cost"] or 0.0
            total["calls"] += 1
            total["tokens_in"] += r["in"]
            total["tokens_out"] += r["out"]
            total["cost"] += r["cost"] or 0.0
            total["unpriced_calls"] += 1 if r["cost"] is None else 0
            total["failures"] += 1 if r.get("error") else 0
        total["cost"] = round(total["cost"], 5)
        for b in list(by_role.values()) + list(by_model.values()):
            b["cost"] = round(b["cost"], 5)
        return {"total": total, "by_role": by_role, "by_model": by_model}


class Router:
    def __init__(self, store: ConfigStore, usage: UsageLog, caller: Callable = call_provider):
        self.store, self.usage, self.caller = store, usage, caller

    def chain(self, role: str) -> list[ModelRef]:
        rc = self.store.get().roles.get(role)
        if not rc or not rc.enabled or not rc.primary:
            return []
        return [rc.primary] + list(rc.fallbacks)

    def enabled(self, role: str) -> bool:
        cfg = self.store.get()
        return any((cfg.provider(m.provider) and cfg.provider(m.provider).api_key) for m in self.chain(role))

    def _cost(self, ref: ModelRef, tin: int, tout: int) -> Optional[float]:
        p = self.store.get().prices.get(ref.key())
        if not p or (p.in_per_m == 0 and p.out_per_m == 0):
            return None
        return tin / 1e6 * p.in_per_m + tout / 1e6 * p.out_per_m

    def check_budget(self) -> None:
        cfg = self.store.get()
        s = self.usage.summary()["total"]
        if s["calls"] >= cfg.budget_calls:
            raise LlmBudgetExceeded("The call budget is used up. Raise it in Models & agents.")
        if cfg.budget_usd is not None and s["cost"] >= cfg.budget_usd:
            raise LlmBudgetExceeded("The spending budget is used up. Raise it in Models & agents.")

    def call(self, role: str, system: str, user: str, *, project: str = "", run_id: str = "", escalate: int = 0,
             max_tokens: Optional[int] = None) -> dict:
        cfg = self.store.get()
        chain = self.chain(role)
        if not chain:
            raise LlmError(f'The "{role}" agent has no model assigned (or is switched off).')
        start = min(max(escalate, 0), len(chain) - 1)
        order = chain[start:] + chain[:start]  # escalated model first, then the rest as failover
        last = ""
        for ref in order:
            prov = cfg.provider(ref.provider)
            if not prov or not prov.api_key:
                last = f"provider {ref.provider} has no key"
                continue
            self.check_budget()
            t0 = time.time()
            try:
                out, u = self.caller(prov.kind, prov.base_url, prov.api_key, ref.model, system, user,
                                     max_tokens or cfg.roles[role].max_tokens, cfg.timeout)
            except LlmError as e:
                self.usage.add({"ts": t0, "role": role, "model": ref.key(), "in": 0, "out": 0, "cost": 0.0, "ms": int((time.time() - t0) * 1000),
                                "project": project, "run": run_id, "error": str(e)[:200]})
                last = str(e)
                continue
            self.usage.add({"ts": t0, "role": role, "model": ref.key(), "in": u["in"], "out": u["out"], "cost": self._cost(ref, u["in"], u["out"]),
                            "ms": int((time.time() - t0) * 1000), "project": project, "run": run_id})
            return out
        raise LlmError(last or "No model could answer.")

    def for_role(self, role: str, project: str = "", run_id: str = "") -> "RoleLlm":
        return RoleLlm(self, role, project, run_id)

    def test(self, provider_id: str, model: str) -> dict:
        cfg = self.store.get()
        prov = cfg.provider(provider_id)
        if not prov:
            return {"ok": False, "error": "unknown provider"}
        t0 = time.time()
        try:
            out, u = self.caller(prov.kind, prov.base_url, prov.api_key, model, 'Reply with the JSON object {"ok": true}', "ping", 200, 40.0)
            return {"ok": True, "ms": int((time.time() - t0) * 1000), "tokens": u, "reply": out}
        except LlmError as e:
            return {"ok": False, "error": str(e)}


class RoleLlm:
    """What an agent holds: complete_json(system, user), bound to one role, one project and one run."""

    def __init__(self, router: Router, role: str, project: str = "", run_id: str = "", escalate: int = 0):
        self.router, self.role, self.project, self.run_id, self.level = router, role, project, run_id, escalate

    def configured(self) -> bool:
        return self.router.enabled(self.role)

    def escalated(self) -> "RoleLlm":
        return RoleLlm(self.router, self.role, self.project, self.run_id, self.level + 1)

    def complete_json(self, system: str, user: str, max_tokens: Optional[int] = None) -> dict:
        return self.router.call(self.role, system, user, project=self.project, run_id=self.run_id, escalate=self.level, max_tokens=max_tokens)
