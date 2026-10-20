"""Deterministic-first routing: rules, then reuse of saved patterns, and only then the language model (D6).

Everything here is plain code. A request that resolves here costs zero LLM calls.
"""
from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from typing import Optional

from .patterns import PatternStore, bind_params, instantiate
from .spec import AddCustomField, ChangeSet, OpAdapter, SetProperty, slugify

QUOTE_RE = re.compile(r"[\"“‘'](.+?)[\"”’']")
TYPE_HINTS = [
    (r"\bdate\s*time\b|\bdatetime\b", "Datetime"), (r"\bdate\b", "Date"), (r"\btime\b", "Time"),
    (r"\bcheck\s*box\b|\bcheckbox\b|\byes\s*/\s*no\b|\bflag\b|\btick\b", "Check"),
    (r"\bcurrency\b|\bamount\b|\bprice\b|\bcost\b", "Currency"), (r"\bpercent(?:age)?\b", "Percent"),
    (r"\bdrop\s*down\b|\bdropdown\b|\bselect\b|\boptions?\b|\bone of\b", "Select"),
    (r"\blong text\b|\bparagraph\b|\bnotes?\b|\bremarks?\b|\bdescription\b|\bcomments?\b", "Small Text"),
    (r"\bnumber\b|\binteger\b|\bcount\b|\bwhole number\b", "Int"), (r"\bdecimal\b|\bfloat\b", "Float"),
    (r"\battach(?:ment)?\b|\bupload\b|\bfile\b", "Attach"), (r"\bphone\b|\bmobile\b", "Phone"),
]
PROP_WORDS = {
    "mandatory": ("reqd", 1), "required": ("reqd", 1), "optional": ("reqd", 0),
    "read only": ("read_only", 1), "readonly": ("read_only", 1), "read-only": ("read_only", 1),
    "editable": ("read_only", 0), "hidden": ("hidden", 1), "visible": ("hidden", 0),
}


@dataclass
class RouteResult:
    kind: str  # rule | pattern | replicate | lookup | llm
    changeset: Optional[ChangeSet] = None
    reply: str = ""
    note: str = ""


def resolve_doctype(text: str, names: list[str], context_doctype: Optional[str]) -> tuple[Optional[str], tuple[int, int]]:
    """Find the DocType the user means: named after to/on/in/for, else the longest name present, else the open page."""
    ordered = sorted(names, key=len, reverse=True)
    if ordered:
        alt = "|".join(re.escape(n) for n in ordered)
        m = re.search(r"\b(?:to|on|in|for|of|into)\s+(?:the\s+)?(" + alt + r")\b(?:\s+(?:form|doctype|screen|page|list))?",
                      text, re.I)
        if m:
            real = next(n for n in ordered if n.lower() == m.group(1).lower())
            return real, m.span()
        m = re.search(r"\b(" + alt + r")\b", text, re.I)
        if m and len(m.group(1)) > 4:
            real = next(n for n in ordered if n.lower() == m.group(1).lower())
            return real, m.span()
    if context_doctype and re.search(r"\b(this|here|current)\b\s*(form|page|doctype|screen|list)?|\bon this\b", text, re.I):
        return context_doctype, (-1, -1)
    if context_doctype:
        return context_doctype, (-1, -1)
    return None, (-1, -1)


def _cut(text: str, span: tuple[int, int]) -> str:
    return text if span[0] < 0 else text[:span[0]] + " § " + text[span[1]:]


def _title(label: str) -> str:
    label = label.strip(" .,:;")
    return label.title() if label == label.lower() else label


def parse_add_field(text: str, doctype: str, span, snap) -> Optional[AddCustomField]:
    if not re.match(r"^\s*(?:please\s+|can you\s+|could you\s+)?(?:add|create|insert|introduce)\b", text, re.I):
        return None
    if not re.search(r"\b(field|column|checkbox|dropdown|drop-down|select)\b", text, re.I):
        return None
    body = _cut(text, span)
    reqd = bool(re.search(r"\b(mandatory|required|compulsory)\b", body, re.I))
    q = QUOTE_RE.search(body)
    label = None
    if q:
        label = q.group(1)
    else:
        for pat in (
            r"\b(?:field|column|checkbox|dropdown)\s+(?:called|named|labell?ed|titled)\s+(.+?)(?=\s+(?:with|after|as|of type)\b|\s*§|$)",
            r"\b(?:add|create|insert|introduce)\s+(?:a|an|the|new)?\s*(?:(?:mandatory|required|compulsory|optional)\s+)?"
            r"(?:(?:date\s*time|datetime|date|time|text|number|integer|decimal|checkbox|currency|dropdown|drop-down|select|link|percent|phone|attachment)\s+)?"
            r"(?:field\s+(?:for\s+)?)?(.+?)\s+(?:field|column)\b",
            r"\b(?:field|checkbox|dropdown)\s+(.+?)(?=\s+(?:with|after|as|of type)\b|\s*§|$)",
        ):
            m = re.search(pat, body, re.I)
            if m and m.group(1).strip():
                label = m.group(1)
                break
    if not label:
        return None
    label = re.sub(r"\b(mandatory|required|compulsory|optional)\b", "", label, flags=re.I).strip()
    label = re.sub(r"^(a|an|the|new)\s+", "", label, flags=re.I).strip()
    if not label or len(label) > 80:
        return None
    label = _title(label)

    ftype, options = "Data", None
    scan = body.replace(q.group(0), " ") if q else body
    for pat, t in TYPE_HINTS:
        if re.search(pat, scan, re.I):
            ftype = t
            break
    link = re.search(r"\b(?:link(?:ed)?\s+to|links?\s+to|reference\s+to)\s+(?:the\s+)?([A-Za-z][A-Za-z0-9 _-]+?)(?=\s+(?:after|on|to|in|for)\b|\s*§|$)", body, re.I)
    if link:
        target = next((n for n in snap.doctype_names() if n.lower() == link.group(1).strip().lower()), None)
        if target:
            ftype, options = "Link", target
    opt = re.search(r"\b(?:options?|values?|one of|choices?)\s*(?:are|:|=)?\s*(.+?)(?=\s+(?:after|on|to|in|for)\b|\s*§|$)", body, re.I)
    if ftype == "Select" or (opt and re.search(r"[,/]", opt.group(1))):
        if opt:
            vals = [v.strip(" .'\"") for v in re.split(r",|/|\bor\b|\band\b", opt.group(1)) if v.strip(" .'\"")]
            if len(vals) >= 2:
                ftype, options = "Select", "\n".join(vals)
        if ftype == "Select" and not options:
            return None  # a dropdown with no options is a question for the user, not a guess
    insert_after = None
    af = re.search(r"\bafter\s+(?:the\s+)?(.+?)(?:\s+field)?(?=\s+(?:on|to|in|for)\b|\s*§|$)", body, re.I)
    if af:
        f = snap.find_field(doctype, af.group(1))
        if f:
            insert_after = f["fieldname"]
    return AddCustomField(doctype=doctype, fieldname=slugify(label, "custom_"), label=label, fieldtype=ftype,
                          options=options, reqd=reqd, insert_after=insert_after)


def parse_make_property(text: str, doctype: str, span, snap) -> Optional[SetProperty]:
    m = re.match(r"^\s*(?:please\s+)?(?:make|set|mark)\s+(?:the\s+)?(.+?)\s+(?:field\s+)?(?:as\s+)?"
                 r"(mandatory|required|optional|read[- ]?only|editable|hidden|visible)\b", _cut(text, span), re.I)
    if not m:
        return None
    word = re.sub(r"\s+", " ", m.group(2).lower())
    word = "read only" if word.startswith("read") else word
    f = snap.find_field(doctype, m.group(1))
    if not f:
        return None
    prop, val = PROP_WORDS[word]
    return SetProperty(doctype=doctype, fieldname=f["fieldname"], property=prop, value=val)


def lookup_fields(text: str, doctype: Optional[str], snap) -> Optional[str]:
    if not re.search(r"\b(what|which|list|show)\b.*\bfields?\b|\bfields?\b.*\b(does|in|of|on)\b.*\b(have|has)\b", text, re.I):
        return None
    if not doctype:
        return "Which DocType do you mean? Open its form or name it."
    m = snap.meta(doctype)
    if not m:
        return f'I could not find a DocType called "{doctype}".'
    rows = [f"{f.get('label') or f['fieldname']} ({f['fieldname']}: {f['fieldtype']})"
            for f in m["fields"] if f.get("fieldname") and f.get("fieldtype") not in ("Section Break", "Column Break", "Tab Break")]
    shown = rows[:70]
    more = f" … and {len(rows) - 70} more" if len(rows) > 70 else ""
    return f"{doctype} has {len(rows)} fields:\n" + "\n".join(f"• {r}" for r in shown) + more


def replicate(text: str, target: str, last: Optional[ChangeSet]) -> Optional[ChangeSet]:
    """'Do the same for Purchase Order': clone the last applied change set onto another DocType."""
    if not last:
        return None
    sources = {getattr(o, "doctype", None) for o in last.ops} - {None}
    if len(sources) != 1:
        return None
    src = sources.pop()
    if src == target:
        return None
    ops = []
    for o in last.ops:
        d = o.model_dump(mode="json", exclude_none=True)
        d = _swap(d, src, target)
        ops.append(OpAdapter.validate_python(d))
    return ChangeSet(title=f"{last.title} (copied to {target})", intent=text, summary=last.summary,
                     source="replicate", ops=ops, pattern_id=last.pattern_id)


def _swap(obj, old, new):
    if isinstance(obj, str):
        return obj.replace(old, new)
    if isinstance(obj, list):
        return [_swap(x, old, new) for x in obj]
    if isinstance(obj, dict):
        return {k: _swap(v, old, new) for k, v in obj.items()}
    return obj


def route(message: str, ctx: dict, snap, patterns: PatternStore, last_applied: Optional[ChangeSet]) -> RouteResult:
    names = snap.doctype_names()
    page_dt = (ctx or {}).get("doctype")
    doctype, span = resolve_doctype(message, names, page_dt)

    ans = lookup_fields(message, doctype, snap)
    if ans:
        return RouteResult("lookup", reply=ans)

    if re.match(r"^\s*(?:please\s+)?(?:do|apply|make|add|create)?\s*(?:the\s+)?(?:same|similar|likewise)\b", message, re.I) and doctype:
        cs = replicate(message, doctype, last_applied)
        if cs:
            return RouteResult("replicate", changeset=cs, note=f"Copied the last applied change to {doctype}.")

    if doctype:
        op = parse_add_field(message, doctype, span, snap) or parse_make_property(message, doctype, span, snap)
        if op:
            return RouteResult("rule", changeset=ChangeSet(
                title=_short(op), intent=message, summary="Built by deterministic rule, no model involved.",
                source="rule", ops=[op]))
    elif re.match(r"^\s*(?:please\s+)?(?:add|create|insert|make)\b", message, re.I) and re.search(r"\bfield\b", message, re.I):
        return RouteResult("lookup", reply="Which DocType should this go on? Open its form first, or name it (for example: … on Sales Order).")

    hit = patterns.best_match(message, names)
    if hit and doctype:
        pat, score = hit
        vals, notes = bind_params(pat, message, snap.roles())
        try:
            ops = instantiate(pat, doctype, vals)
        except Exception:
            ops = None
        if ops:
            return RouteResult("pattern", changeset=ChangeSet(
                title=f"{pat.name} on {doctype}", intent=message, summary=pat.description, source="pattern",
                ops=ops, pattern_id=pat.id, warnings=notes), note=f"Matched saved pattern “{pat.name}” ({score:.0%}).")
    return RouteResult("llm")


def _short(op) -> str:
    if op.op == "add_custom_field":
        return f'Add field "{op.label}" to {op.doctype}'
    return f"Set {op.property} on {op.fieldname} of {op.doctype}"
