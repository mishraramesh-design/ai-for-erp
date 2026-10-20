"""Deterministic first pass over requirements: no model. Strong signatures of work outside the studio's operations are
rejected with a reason; weaker signals are passed to the agents as hints."""
from __future__ import annotations

import re

OUT_OF_SCOPE = [
    (r"\bprint\s*formats?\b|\bletter\s*head\b|\bprinted\s+(?:copy|output)\b", "Print formats are not an operation the studio can build yet."),
    (r"\bmobile\s+app\b|\bandroid\b|\bios\s+app\b", "A mobile app is outside configuration of ERPNext."),
    (r"\bsms\b|\bwhats\s*app\b|\bbiometric\b|\bbarcode\s+scanner\b|\brfid\b|\bgps\b", "Needs an external device or gateway integration."),
    (r"\bintegrat(?:e|ion|ed)\b.{0,60}\b(?:with|to)\b.{0,40}\b(?:external|third|bank|gst portal|e-?invoice|api|sap|tally|payment gateway)\b", "Needs an external system integration."),
    (r"\bsso\b|\bldap\b|\bactive directory\b|\bsingle sign", "Identity integration is outside the studio's operations."),
    (r"\bdata\s+migration\b|\blegacy\s+data\b|\bmigrat(?:e|ion)\s+(?:of\s+)?(?:existing|historical)\b", "Data migration is a separate activity."),
    (r"\bdigital\s+signature\b|\bdsc\b|\bblockchain\b|\bocr\b|\bartificial intelligence\b|\bmachine learning\b", "Needs a specialised component, not configuration."),
]
HINTS = [
    (r"\breports?\b|\bmis\b|\banalytics?\b|\bcharts?\b|\bgraphs?\b", "reporting"),
    (r"\bapproval\b|\bapprove\b|\bworkflow\b|\bescalat", "approval/workflow"),
    (r"\bemail\b|\bnotif|\balert\b|\breminder\b", "notification"),
    (r"\bauthori[sz]ation\b|\bpermission\b|\baccess\b|\brole\b", "permissions"),
    (r"\bmandatory\b|\bvalidat|\bcannot\b|\bshould not\b|\bblock\b|\brestrict", "validation"),
]


def triage(text: str) -> tuple[str, str]:
    t = (text or "").lower()
    for pat, why in OUT_OF_SCOPE:
        # Long requirements usually mix several asks; one external clause must not discard the configurable rest.
        if re.search(pat, t) and len(t) < 260:
            return "out_of_scope", why
    return "candidate", ""


def external_clauses(text: str) -> list[str]:
    t = (text or "").lower()
    return [why for pat, why in OUT_OF_SCOPE if re.search(pat, t)]


def hints(text: str) -> list[str]:
    t = (text or "").lower()
    return [name for pat, name in HINTS if re.search(pat, t)]
