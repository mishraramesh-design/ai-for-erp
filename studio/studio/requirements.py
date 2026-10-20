"""Requirement register: deterministic import of the RFP fitment matrix and traceability to change sets (R8, R15).

Every requirement keeps its life-cycle status, so the studio can answer 'what is built, by which change, and who verified it'.
"""
from __future__ import annotations

import io
import json
import re
import threading
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, Field

Status = Literal["fit", "backlog", "planned", "built", "verified", "blocked", "standard", "needs_input"]
SKIP_SHEETS = ("cover", "b17-a summary", "tech marks", "non-core")
ID_RE = re.compile(r"^\s*\[(\d+)\]\s*")
JUNK = "• ​"


class Requirement(BaseModel):
    id: int
    module: str  # module name as in the RFP, e.g. "Supply Chain & Inventory Mgmt"
    sheet: str
    row: int
    branch: str = ""
    process: str = ""
    text: str
    erp_module: str = ""
    std: Literal["Yes", "Partial", "No"] = "Yes"
    customization: bool = False
    remarks: str = ""
    how: str = ""
    poc: bool = False
    truncated: bool = False
    status: Status = "fit"
    change_sets: list[str] = Field(default_factory=list)
    notes: str = ""
    evidence: list[dict] = Field(default_factory=list)  # verification checks that passed or failed, with run ids
    verified_by: str = ""

    @property
    def is_gap(self) -> bool:
        return self.std != "Yes"


def _clean(s) -> str:
    t = str(s or "")
    for ch in JUNK:
        t = t.replace(ch, " ")
    return re.sub(r"[ \t]+", " ", t).strip()


def _module_title(ws) -> str:
    raw = _clean(ws.cell(1, 1).value)
    after = raw.split(":", 1)[1] if ":" in raw else ws.title
    after = re.split(r"\*{2,}", after)[0]
    return after.strip() or ws.title


def _poc_modules(wb) -> set:
    out: set = set()
    if "Cover" not in wb.sheetnames:
        return out
    for r in wb["Cover"].iter_rows(values_only=True):
        cells = [_clean(c) for c in r]
        if len(cells) >= 4 and cells[3] == "PoC" and cells[1]:
            out.add(cells[1].lower())
    return out


def _looks_truncated(text: str) -> bool:
    return len(text) >= 200 and text[-1] not in ".?!)\"'”;:"


def parse_matrix(data: bytes) -> list[Requirement]:
    import openpyxl  # imported here so the rest of the studio runs without it

    wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=False)
    poc = _poc_modules(wb)
    out: list[Requirement] = []
    seen: set = set()
    for ws in wb.worksheets:
        if any(ws.title.lower().startswith(s) for s in SKIP_SHEETS):
            continue
        header = [_clean(c.value).lower() for c in ws[3]] if ws.max_row >= 3 else []
        if not header or "functional requirement" not in " ".join(header):
            continue
        module = _module_title(ws)
        is_poc = module.lower() in poc or "poc" in _clean(ws.cell(1, 1).value).lower()
        for idx, r in enumerate(ws.iter_rows(min_row=4, values_only=True), start=4):
            if r[0] is None or not str(r[0]).strip().isdigit():
                continue  # branch/process banner rows
            raw = _clean(r[2])
            m = ID_RE.match(raw)
            if not m:
                continue
            rid = int(m.group(1))
            if rid in seen:
                continue
            seen.add(rid)
            text = ID_RE.sub("", raw).strip()
            bp = [p.strip() for p in str(r[1] or "").split("\n") if p.strip()]
            std = _clean(r[4]) if _clean(r[4]) in ("Yes", "Partial", "No") else "Yes"
            cust = _clean(r[5]).lower() == "yes"
            out.append(Requirement(
                id=rid, module=module, sheet=ws.title, row=idx, branch=bp[0] if bp else "",
                process=bp[1] if len(bp) > 1 else "", text=text, erp_module=_clean(r[3]), std=std,
                customization=cust, remarks=_clean(r[6]), how=_clean(r[7]), poc=is_poc,
                truncated=_looks_truncated(text), status="fit" if std == "Yes" else "backlog"))
    return out


class RequirementStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._items: dict[int, Requirement] = {}
        if self.path.exists():
            try:
                for d in json.loads(self.path.read_text(encoding="utf-8")):
                    r = Requirement.model_validate(d)
                    self._items[r.id] = r
            except Exception:
                self._items = {}

    def _save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps([r.model_dump(mode="json") for r in self._items.values()]), encoding="utf-8")
        tmp.replace(self.path)

    # ---------------------------------------------------------------- import
    def import_matrix(self, data: bytes) -> dict:
        parsed = parse_matrix(data)
        if not parsed:
            raise ValueError("No requirement rows found. Expected the RFP fitment layout: Sr#, Branch/Process, "
                             "Functional Requirement [n], ERPNext Module, Standard, Customization, Remarks, How.")
        with self._lock:
            added = updated = 0
            for r in parsed:
                old = self._items.get(r.id)
                if old:  # keep progress; refresh the descriptive columns
                    keep = {"status": old.status, "change_sets": old.change_sets, "notes": old.notes}
                    self._items[r.id] = r.model_copy(update=keep)
                    updated += 1
                else:
                    self._items[r.id] = r
                    added += 1
            self._save()
        return {"added": added, "updated": updated, "total": len(self._items)}

    def import_text(self, text: str, module: str, source: str = "upload") -> dict:
        """Free-text functional requirements (a Word file, a pasted list): one requirement per meaningful line."""
        lines = [re.sub(r"^\s*(?:[-*\u2022]|\d+[.)])\s*", "", ln).strip() for ln in (text or "").splitlines()]
        lines = [ln for ln in lines if len(ln) >= 15]
        with self._lock:
            have = {(r.module, r.text) for r in self._items.values()}
            nxt = max([100000] + list(self._items)) + 1
            added = 0
            for ln in lines:
                if (module, ln) in have:
                    continue
                self._items[nxt] = Requirement(id=nxt, module=module, sheet=source, row=added + 1, text=ln, std="No",
                                               customization=True, status="backlog")
                nxt += 1
                added += 1
            self._save()
        return {"added": added, "skipped": len(lines) - added, "total": len(self._items)}

    # ---------------------------------------------------------------- queries
    def get(self, rid: int) -> Optional[Requirement]:
        return self._items.get(rid)

    def all(self) -> list[Requirement]:
        return sorted(self._items.values(), key=lambda r: r.id)

    def stats(self) -> dict:
        mods: dict = {}
        for r in self._items.values():
            m = mods.setdefault(r.module, {"module": r.module, "poc": r.poc, "total": 0, "fit": 0, "gap": 0,
                                          "partial": 0, "no": 0, "planned": 0, "built": 0, "verified": 0, "blocked": 0, "standard": 0, "needs_input": 0})
            m["total"] += 1
            m["fit" if r.std == "Yes" else "gap"] += 1
            if r.std == "Partial":
                m["partial"] += 1
            if r.std == "No":
                m["no"] += 1
            if r.is_gap and r.status in ("planned", "built", "verified", "blocked", "standard", "needs_input"):
                m[r.status] += 1
        rows = sorted(mods.values(), key=lambda m: (not m["poc"], m["module"]))
        total = {k: sum(m[k] for m in rows) for k in ("total", "fit", "gap", "partial", "no", "planned", "built", "verified", "blocked", "standard", "needs_input")}
        return {"modules": rows, "totals": total,
                "truncated_texts": sum(1 for r in self._items.values() if r.truncated)}

    def query(self, module: str = "", gap_only: bool = False, status: str = "", q: str = "",
              branch: str = "", process: str = "", limit: int = 50, offset: int = 0) -> dict:
        items = self.all()
        if module:
            items = [r for r in items if r.module == module]
        if gap_only:
            items = [r for r in items if r.is_gap]
        if status:
            items = [r for r in items if r.status == status]
        if branch:
            items = [r for r in items if r.branch == branch]
        if process:
            items = [r for r in items if r.process == process]
        if q:
            ql = q.lower()
            items = [r for r in items if ql in r.text.lower() or str(r.id) == q.strip()]
        total = len(items)
        return {"total": total, "items": [r.model_dump(mode="json") for r in items[offset:offset + limit]]}

    def groups(self, module: str) -> list[dict]:
        g: dict = {}
        for r in self.all():
            if r.module != module or not r.is_gap:
                continue
            e = g.setdefault((r.branch, r.process), {"branch": r.branch, "process": r.process, "ids": [], "backlog": 0, "done": 0})
            e["ids"].append(r.id)
            if r.status in ("backlog", "blocked"):
                e["backlog"] += 1
            else:
                e["done"] += 1
        return sorted(g.values(), key=lambda e: min(e["ids"]))

    def batch(self, ids: Optional[list[int]] = None, module: str = "", branch: str = "", process: str = "",
              limit: int = 12) -> list[Requirement]:
        if ids:
            return [self._items[i] for i in ids if i in self._items][:limit]
        out = [r for r in self.all() if r.is_gap and r.status == "backlog"
               and (not module or r.module == module) and (not branch or r.branch == branch)
               and (not process or r.process == process)]
        return out[:limit]

    # ---------------------------------------------------------------- life-cycle
    def mark(self, ids: list[int], status: Status, change_set_id: str = "", note: str = "",
             evidence: Optional[list] = None, by: str = "") -> int:
        n = 0
        with self._lock:
            for i in ids:
                r = self._items.get(i)
                if not r:
                    continue
                r.status = status
                if change_set_id and change_set_id not in r.change_sets and status in ("planned", "built"):
                    r.change_sets.append(change_set_id)
                if status == "backlog":
                    if change_set_id and change_set_id in r.change_sets:
                        r.change_sets.remove(change_set_id)
                    r.evidence, r.verified_by = [], ""
                if note:
                    r.notes = note
                if evidence is not None:
                    r.evidence = evidence
                if by:
                    r.verified_by = by
                n += 1
            self._save()
        return n
