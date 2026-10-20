"""Append-style customization ledger: one JSON file per change set, named by its sortable id (Charter D15)."""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Optional

from .spec import ChangeSet


class Ledger:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _path(self, cs_id: str) -> Path:
        if not cs_id.isalnum():
            raise ValueError("bad change-set id")
        return self.root / f"{cs_id}.json"

    def save(self, cs: ChangeSet) -> None:
        with self._lock:
            tmp = self._path(cs.id).with_suffix(".tmp")
            tmp.write_text(cs.model_dump_json(indent=2), encoding="utf-8")
            os.replace(tmp, self._path(cs.id))

    def get(self, cs_id: str) -> Optional[ChangeSet]:
        try:
            p = self._path(cs_id)
        except ValueError:
            return None
        if not p.exists():
            return None
        return ChangeSet.model_validate_json(p.read_text(encoding="utf-8"))

    def list(self, limit: int = 50) -> list:
        files = sorted(self.root.glob("*.json"), reverse=True)[:limit]
        out = []
        for f in files:
            try:
                out.append(ChangeSet.model_validate_json(f.read_text(encoding="utf-8")))
            except Exception:  # a corrupt file must never break the studio
                continue
        return out

    def last_applied(self, session_id: str = "", user: str = "") -> Optional[ChangeSet]:
        for cs in self.list(limit=200):
            if cs.status != "applied":
                continue
            if (session_id and cs.session_id == session_id) or (user and cs.created_by == user):
                return cs
        return None

    def metrics(self) -> dict:
        by_source: dict = {}
        llm_calls = 0
        applied = 0
        for cs in self.list(limit=1000):
            by_source[cs.source] = by_source.get(cs.source, 0) + 1
            llm_calls += cs.llm_calls
            applied += 1 if cs.status == "applied" else 0
        total = sum(by_source.values())
        zero = total - by_source.get("llm", 0) - by_source.get("plan", 0)
        return {"change_sets": total, "applied": applied, "by_source": by_source, "llm_calls": llm_calls,
                "zero_llm_share": round(zero / total, 2) if total else None}


def dump(obj) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False, default=str)
