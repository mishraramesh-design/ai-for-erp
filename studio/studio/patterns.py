"""Pattern library: approved, reusable change sets that later requests resolve with zero LLM calls (Charter D14).

Learning here means accumulating reviewed knowledge, never training weights. Promotion is always a human action.
"""
from __future__ import annotations

import ast
import copy
import json
import re
import threading
import time
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field

from .spec import ChangeSet, OpAdapter, new_id

STOPWORDS = set("""the a an to on in for of if unless and or with when that is are be it this same rule do add make
please can you i we want need should must not no yes from by at as any all""".split())
CONST_LINE = re.compile(r"^([A-Z][A-Z0-9_]*)\s*=\s*(.+?)\s*(?:#.*)?$")
MULT = {"k": 1e3, "lakh": 1e5, "lakhs": 1e5, "lac": 1e5, "crore": 1e7, "cr": 1e7, "m": 1e6, "million": 1e6}
NUM_RE = re.compile(r"(?<![\w.])(\d[\d,]*(?:\.\d+)?)\s*(k|lakhs?|lac|crore|cr|m|million)?\b", re.I)


class Param(BaseModel):
    name: str
    type: str  # number | string | role
    default: Any


class Pattern(BaseModel):
    id: str = Field(default_factory=new_id)
    name: str
    description: str = ""
    created_from: str = ""
    source_doctype: str = ""
    source_intent: str = ""
    tokens: list[str] = Field(default_factory=list)
    params: list[Param] = Field(default_factory=list)
    ops: list[dict] = Field(default_factory=list)  # templated: "{{doctype}}" and "{{PARAMS}}" markers
    created_at: float = Field(default_factory=time.time)
    uses: int = 0


def tokens_of(text: str, drop: list[str] | None = None) -> list[str]:
    t = (text or "").lower()
    for d in sorted(drop or [], key=len, reverse=True):
        t = t.replace(d.lower(), " ")
    t = re.sub(r"\d[\d,\.]*\s*(k|lakhs?|lac|crore|cr|m|million)?", " ", t)
    words = re.findall(r"[a-z]{3,}", t)
    return sorted({w for w in words if w not in STOPWORDS})


def parse_numbers(text: str) -> list:
    out = []
    for m in NUM_RE.finditer(text or ""):
        raw = m.group(1).replace(",", "")
        try:
            val = float(raw)
        except ValueError:
            continue
        mult = MULT.get((m.group(2) or "").lower(), 1)
        val *= mult
        out.append(int(val) if float(val).is_integer() else val)
    return out


def find_roles(text: str, roles) -> list[str]:
    found = []
    low = (text or "").lower()
    for r in sorted(roles, key=len, reverse=True):
        if re.search(r"\b" + re.escape(r.lower()) + r"\b", low) and not any(r in f for f in found):
            found.append(r)
    return found


def _extract_consts(script: str, roles) -> tuple[list[Param], str]:
    lines = script.splitlines()
    params: list[Param] = []
    i = 0
    while i < len(lines):
        s = lines[i].strip()
        if not s or s.startswith("#"):
            i += 1
            continue
        m = CONST_LINE.match(s)
        if not m:
            break
        try:
            val = ast.literal_eval(m.group(2))
        except Exception:
            break
        typ = "number" if isinstance(val, (int, float)) and not isinstance(val, bool) else "string"
        if typ == "string" and val in roles:
            typ = "role"
        if isinstance(val, bool):
            break
        params.append(Param(name=m.group(1), type=typ, default=val))
        i += 1
    rest = "\n".join(lines[i:]).lstrip("\n")
    return params, rest


def _sub_strings(obj, old: str, new: str):
    if isinstance(obj, str):
        return obj.replace(old, new)
    if isinstance(obj, list):
        return [_sub_strings(x, old, new) for x in obj]
    if isinstance(obj, dict):
        return {k: _sub_strings(v, old, new) for k, v in obj.items()}
    return obj


def promote(cs: ChangeSet, name: str, roles) -> Pattern:
    doctypes = sorted({getattr(o, "doctype", None) or getattr(o, "name", "") for o in cs.ops
                       if o.op != "create_role"} - {""})
    if len(doctypes) != 1 or cs.ops[0].op == "create_doctype":
        raise ValueError("Only change sets that target a single existing DocType can become patterns.")
    src = doctypes[0]
    ops = [o.model_dump(mode="json", exclude_none=True) for o in cs.ops]
    params: list[Param] = []
    for d in ops:
        if d["op"] == "add_server_validation":
            ps, rest = _extract_consts(d["script"], roles)
            if ps:
                params.extend(ps)
                d["script"] = "{{PARAMS}}\n" + rest
    ops = _sub_strings(ops, src, "{{doctype}}")
    return Pattern(name=name, description=cs.summary or cs.title, created_from=cs.id, source_doctype=src,
                   source_intent=cs.intent, tokens=tokens_of(cs.intent, [src]), params=params, ops=ops)


def _render_params(params: list[Param], values: dict) -> str:
    return "\n".join(f"{p.name} = {values.get(p.name, p.default)!r}" for p in params)


def instantiate(p: Pattern, doctype: str, values: dict) -> list:
    ops = []
    block = _render_params(p.params, values)
    for d in copy.deepcopy(p.ops):
        if "script" in d:
            d["script"] = d["script"].replace("{{PARAMS}}", block)
        d = _sub_strings(d, "{{doctype}}", doctype)
        ops.append(OpAdapter.validate_python(d))
    return ops


def bind_params(p: Pattern, message: str, roles) -> tuple[dict, list[str]]:
    values: dict = {}
    notes: list[str] = []
    nums = parse_numbers(message)
    found_roles = find_roles(message, roles)
    quoted = re.findall(r"[\"'“‘](.+?)[\"'”’]", message)
    for prm in p.params:
        if prm.type == "number" and nums:
            values[prm.name] = nums.pop(0)
        elif prm.type == "role" and found_roles:
            values[prm.name] = found_roles.pop(0)
        elif prm.type == "string" and quoted:
            values[prm.name] = quoted.pop(0)
        else:
            notes.append(f"{prm.name} not given, using the saved default {prm.default!r}")
    return values, notes


class PatternStore:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def save(self, p: Pattern) -> None:
        with self._lock:
            (self.root / f"{p.id}.json").write_text(p.model_dump_json(indent=2), encoding="utf-8")

    def get(self, pid: str) -> Optional[Pattern]:
        f = self.root / f"{pid}.json"
        return Pattern.model_validate_json(f.read_text(encoding="utf-8")) if pid.isalnum() and f.exists() else None

    def all(self) -> list[Pattern]:
        out = []
        for f in sorted(self.root.glob("*.json")):
            try:
                out.append(Pattern.model_validate_json(f.read_text(encoding="utf-8")))
            except Exception:
                continue
        return out

    def best_match(self, message: str, doctype_names: list[str]) -> Optional[tuple[Pattern, float]]:
        want = set(tokens_of(message, doctype_names))
        best: Optional[tuple[Pattern, float]] = None
        for p in self.all():
            have = set(p.tokens)
            if not have or not want:
                continue
            inter = want & have
            score = len(inter) / len(want | have)
            if len(inter) >= 2 and score >= 0.5 and (best is None or score > best[1]):
                best = (p, score)
        return best

    def mark_used(self, p: Pattern) -> None:
        p.uses += 1
        self.save(p)


def dump(p: Pattern) -> str:
    return json.dumps(p.model_dump(mode="json"), indent=2)
