"""The only place the language model is used (D6). It returns typed specs; validators and the applier do the rest.

propose(): a chat request that the rules and patterns could not handle.
plan_batch(): a group of RFP gap requirements -> one change set plus an honest list of what it could not cover.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Optional

from pydantic import ValidationError

from .llm import LlmError
from .spec import ChangeSet, LlmProposal, FIELD_TYPES
from .validators import PROPERTY_TYPES, SETTING_DOCTYPES, Issue, has_errors, order_ops, validate

SPEC_HELP = f"""
You output ONE JSON object:
{{"reply": str, "title": str, "summary": str, "questions": [str], "assumptions": [str],
  "covered": [requirement ids fully handled by ops], "not_covered": [{{"id": int, "reason": str}}], "ops": [op, ...]}}
"ops" may be empty (then just answer in "reply"). Each op has an "op" key and ONLY these shapes:
 add_custom_field {{doctype, fieldname (custom_ prefix, snake_case), label, fieldtype, insert_after?, options?, reqd?, default?, description?, in_list_view?, depends_on?}}
 set_property {{doctype, fieldname? (omit for form-level), property in {sorted(PROPERTY_TYPES)}, value}}
 add_client_script {{doctype, name, script}}  (JavaScript using frappe.ui.form.on('<DocType>', {{...}}); no fetch, no external URLs)
 add_server_validation {{doctype, event in before_insert|before_validate|before_save|after_insert|after_save|before_submit|after_submit|before_cancel|after_cancel|before_delete|after_delete, name, script}}
   (Frappe restricted Python: NO imports; use frappe.throw(), frappe.db.get_value(), frappe.utils.* ; read fields as doc.<fieldname>; keep tunable numbers as UPPERCASE constants at the top;
    NEVER use getattr, setattr, hasattr-style tricks, underscore-prefixed names, imports, open, eval or exec: use doc.get('field'), doc.fieldname, frappe.db.get_value, frappe.utils.getdate/flt/cint)
 set_setting {{doctype in {sorted(SETTING_DOCTYPES)}, field, value}}
 create_role {{role_name}}
 add_permission {{doctype, role, permlevel?, read, write, create, delete, submit, cancel, amend, report, export, print, email, share}}
 create_doctype {{name, module? (default "Custom"), description?, is_child_table?, is_submittable?, naming: series|field|hash|prompt, series_prefix?, name_field?, title_field?, fields: [{{fieldname, label, fieldtype, options?, reqd?, unique?, in_list_view?, in_standard_filter?, default?, depends_on?, fetch_from?}}], permissions: [{{role, read, write, create, delete, submit, cancel, amend, report, export, print, email, share}}]}}
 create_workflow {{name, doctype, states: [{{state, doc_status 0|1|2, allow_edit (role)}}], transitions: [{{state, action, next_state, allowed (role), allow_self_approval?, condition?}}]}}
 create_notification {{name, doctype, event New|Save|Submit|Cancel|Value Change|Days Before|Days After, subject, message (HTML; Jinja like {{{{ doc.supplier_name }}}}), channel? Email|System Notification, condition? (Python expression on doc e.g. doc.status == "Approved"), value_changed? (field, for Value Change), date_field? and days? (for Days Before/After), recipients: [{{field? (document field holding the email/user), role?}}]}}
 create_report {{name, doctype, columns: [fieldnames], filters?: [{{field, operator = != > < >= <= like in, value}}], sort_by?, sort_desc?, roles?}}   (Report Builder list report with chosen columns)
 create_number_card {{label, doctype, function? Count|Sum|Average|Minimum|Maximum, field? (numeric, for non-Count), filters?, color?}}
 create_dashboard_chart {{name, doctype, chart_type? Group By|Count|Sum|Average, group_by_field? (Group By), date_field? (time axis for Count/Sum/Average), value_field?, graph? Bar|Line|Pie|Donut|Percentage, interval?, timespan?, filters?}}
 create_dashboard {{name, charts: [chart names created or existing], cards: [number card labels]}}   (shows charts and cards together at /app/dashboard-view/<name>)
 create_html_block {{name, html, style?}}   (static banner/branding block for a workspace; no scripts, no external resources)
 create_workspace {{title, module? (default Buying), html_blocks: [block names], cards: [number card labels], charts: [chart names], shortcuts: [{{label, doctype, view? List|New|Report Builder|Dashboard}}], roles?}}   (a landing page; create blocks/cards/charts earlier in the same ops list)
Field types: {", ".join(FIELD_TYPES)}. Link fields need options = an EXISTING DocType name. Select options are newline-separated.
Rules:
 - Use ONLY DocTypes, fields and roles that appear in the context below, or ones you create in the same ops list (create_role before use).
 - Prefer the smallest change. Prefer configuration (set_property, set_setting, custom field) over scripts; prefer standard ERPNext features over new DocTypes.
 - Never invent a field of a standard DocType; if you need it, add a custom field.
 - If a requirement says a value must be recorded/captured/mandatory, set reqd=true on that field. A conditional rule (mandatory only when X) needs BOTH mandatory_depends_on AND a server validation, because the API does not enforce mandatory_depends_on.
 - Workflows: the first state must have doc_status 0; submitted states doc_status 1; only for submittable DocTypes use 1/2.
 - If a requirement needs something the ops cannot express (integrations, print formats, mobile, external systems, data migration, UI beyond forms, complex analytical reports that need joins or calculations), do NOT fake it: put its id in not_covered with a one-line reason.
 - If a decision is genuinely ambiguous, state your assumption in "assumptions" (do not block); only use "questions" if you cannot proceed.
 - Output JSON only.
""".strip()


@dataclass
class AgentResult:
    escalated: bool = False
    reply: str = ""
    changeset: Optional[ChangeSet] = None
    llm_calls: int = 0
    issues: list = field(default_factory=list)
    error: str = ""


def _fields_line(snap, doctype: str, limit: int = 90) -> str:
    m = snap.meta(doctype)
    if not m:
        return f"{doctype}: (not found)"
    rows = [f"{f['fieldname']}:{f['fieldtype']}" + (f"→{f['options']}" if f.get("fieldtype") in ("Link", "Table") and f.get("options") else "")
            for f in m["fields"] if f.get("fieldname") and f.get("fieldtype") not in ("Section Break", "Column Break", "Tab Break")]
    sub = " [submittable]" if m.get("is_submittable") else ""
    return f"{doctype}{sub}: " + ", ".join(rows[:limit]) + (" …" if len(rows) > limit else "")


def build_context(message: str, ctx: dict, snap, extra_doctypes: Optional[list] = None) -> str:
    names = snap.doctype_names()
    lower = message.lower()
    want: list = []
    page = (ctx or {}).get("doctype")
    if page and snap.has_doctype(page):
        want.append(page)
    for n in sorted(names, key=len, reverse=True):
        if len(n) > 4 and n.lower() in lower and n not in want:
            want.append(n)
        if len(want) >= 4:
            break
    for n in extra_doctypes or []:
        if n in names and n not in want and len(want) < 7:
            want.append(n)
    parts = ["DOCTYPE FIELDS (fieldname:type→link target):"] + [_fields_line(snap, n) for n in want]
    parts.append("CUSTOM DOCTYPES ALREADY CREATED: " + (", ".join(snap.custom_doctypes()[:60]) or "none"))
    parts.append("ROLES: " + ", ".join(sorted(snap.roles())[:150]))
    parts.append("MODULES: " + ", ".join(sorted(snap.modules())[:60]))
    parts.append("ALL DOCTYPE NAMES (for Link targets): " + ", ".join(names[:700]))
    if ctx:
        pc = {k: v for k, v in ctx.items() if k in ("doctype", "docname", "view", "route", "workflow_state") and v}
        if pc:
            parts.append("USER IS CURRENTLY ON: " + json.dumps(pc))
    return "\n".join(parts)


def _issues_text(issues: list) -> str:
    return "\n".join(f"- [{i.level}] op {('#' + str(i.op_index + 1)) if i.op_index >= 0 else '-'}: {i.message}"
                     for i in issues if i.level == "error")


def _run(llm, system: str, user: str, snap, source: str, message: str, created_by: str,
         session_id: str, requirement_ids: Optional[list] = None, repairs: int = 2) -> AgentResult:
    res = AgentResult()
    prompt = user
    prop: Optional[LlmProposal] = None
    cs: Optional[ChangeSet] = None
    for attempt in range(repairs + 1):
        use = llm
        if attempt > 0 and attempt == repairs and hasattr(llm, "escalated"):
            use = llm.escalated()  # last try goes to the stronger model in the chain, if there is one
            res.escalated = True
        try:
            raw = use.complete_json(system, prompt)
            res.llm_calls += 1
        except LlmError as e:
            res.error = str(e)
            res.reply = f"I could not reach the language model: {e}"
            return res
        try:
            prop = LlmProposal.model_validate(raw)
        except ValidationError as e:
            msg = "; ".join(f"{'.'.join(str(x) for x in er['loc'])}: {er['msg']}" for er in e.errors()[:8])
            prompt = f"{user}\n\nYOUR PREVIOUS ANSWER WAS NOT VALID AGAINST THE SCHEMA:\n{msg}\nReturn corrected JSON."
            res.reply = "The model returned a malformed change; retrying."
            continue
        if not prop.ops:
            res.reply = prop.reply or "Nothing to change."
            res.changeset = None
            if prop.questions:
                res.reply += "\n\nQuestions:\n" + "\n".join(f"• {q}" for q in prop.questions)
            if prop.not_covered:
                res.changeset = ChangeSet(title=prop.title or "Not coverable by configuration", intent=message, summary=prop.summary,
                                          source=source, ops=[], not_covered=prop.not_covered, llm_calls=res.llm_calls,
                                          created_by=created_by, session_id=session_id, requirement_ids=requirement_ids or [])
            return res
        ordered, _ = order_ops(prop.ops)
        cs = ChangeSet(title=prop.title or "Proposed change", intent=message, summary=prop.summary or prop.reply, source=source,
                       ops=ordered, created_by=created_by, session_id=session_id, requirement_ids=list(prop.covered),
                       assumptions=prop.assumptions, questions=prop.questions, not_covered=prop.not_covered)
        issues = validate(cs, snap)
        cs.issues = [i.to_dict() for i in issues]
        if not has_errors(issues):
            break
        prompt = (f"{user}\n\nYOUR PREVIOUS ANSWER FAILED VALIDATION. Fix these errors and return the full corrected JSON:\n"
                  f"{_issues_text(issues)}\nPREVIOUS OPS:\n{json.dumps(raw.get('ops'), ensure_ascii=False)[:6000]}")
    if cs is None:
        res.reply = res.reply or "I could not produce a valid change."
        return res
    cs.llm_calls = res.llm_calls
    res.changeset = cs
    res.issues = cs.issues
    bad = [i for i in cs.issues if i["level"] == "error"]
    res.reply = (prop.reply if prop else "") or cs.summary or "Here is a proposed change."
    if bad:
        res.reply += "\n\nI could not make this pass validation, so it cannot be applied as is."
    return res


def propose(message: str, ctx: dict, history: list, snap, llm, created_by: str = "", session_id: str = "") -> AgentResult:
    system = ("You are the ERPNext v15 configuration and customization engineer inside a governed studio. "
              "You never change anything yourself: you return a typed change specification that deterministic code validates and applies.\n" + SPEC_HELP)
    hist = "\n".join(f"{h.get('role')}: {str(h.get('content'))[:600]}" for h in (history or [])[-6:])
    user = f"{build_context(message, ctx, snap)}\n\nRECENT CHAT:\n{hist or '(none)'}\n\nUSER REQUEST:\n{message}"
    return _run(llm, system, user, snap, "llm", message, created_by, session_id)


def _req_block(r, hint: str = "") -> str:
    t = r.text + (" [TEXT IS CUT OFF IN THE SOURCE FILE]" if r.truncated else "")
    extra = f"\n   analyst note: {hint}" if hint else ""
    return (f"[{r.id}] ({r.std}) {t}\n   process: {r.branch} / {r.process}; RFP says: {r.remarks[:200] or '-'}; approach: {r.how[:200] or '-'}" + extra)


def plan_batch(reqs: list, snap, llm, guidance: str = "", created_by: str = "", session_id: str = "",
               hints: Optional[dict] = None, feedback: str = "") -> AgentResult:
    ids = [r.id for r in reqs]
    system = ("You are the ERPNext v15 solution engineer closing fit-gap items from an RFP. For each requirement below, decide what ERPNext "
              "configuration or customization (using ONLY the ops) would satisfy it, and combine them into one coherent change set. "
              "Be honest: list in not_covered anything the ops cannot do.\n" + SPEC_HELP)
    mods = " ".join(r.text for r in reqs)
    user = (build_context(mods, {}, snap) + "\n\nREQUIREMENTS (the number in brackets is the id to use in covered / not_covered):\n"
            + "\n".join(_req_block(r, (hints or {}).get(r.id, "")) for r in reqs)
            + (f"\n\nPROJECT GUIDANCE FROM THE ARCHITECT:\n{guidance}" if guidance else "")
            + (f"\n\nFEEDBACK ON YOUR PREVIOUS ATTEMPT (fix these; do not repeat them):\n{feedback}" if feedback else ""))
    res = _run(llm, system, user, snap, "plan", f"Close {len(ids)} gap requirement(s): {ids}", created_by, session_id, requirement_ids=ids)
    if res.changeset:
        known = set(ids)
        res.changeset.requirement_ids = [i for i in res.changeset.requirement_ids if i in known]
        accounted = set(res.changeset.requirement_ids) | {int(n.get("id")) for n in res.changeset.not_covered if str(n.get("id", "")).lstrip("-").isdigit()}
        for i in ids:
            if i not in accounted:
                res.changeset.not_covered.append({"id": i, "reason": "The model did not address this requirement."})
    return res


# ---------------------------------------------------------------------------------------------------------- analyst
CLASSES = ("configure", "customize", "standard", "out_of_scope", "clarify")


def analyst(reqs: list, snap, llm) -> dict:
    """Cheap triage. Returns {id: {"class", "reason", "approach", "question"}}; missing ids default to 'configure'."""
    system = ("You are a senior ERPNext v15 business analyst triaging RFP fit-gap items. For EACH requirement choose one class:\n"
              " configure   = achievable with settings, custom fields, property changes, roles/permissions, or a workflow\n"
              " customize   = needs a script or a new DocType, still inside the form layer\n"
              " configure also covers: list reports, email/system notifications, count cards and charts, dashboards\n"
              " standard    = ERPNext already does this out of the box (say where)\n"
              " out_of_scope= needs print formats, external integrations, devices, migration, or custom code outside forms\n"
              " clarify     = cannot be built without a business decision (ask ONE precise question)\n"
              'Return JSON: {"items":[{"id":int,"class":str,"reason":str,"approach":str (one line, the ERPNext way),"question":str}]}. JSON only.')
    user = (build_context(" ".join(r.text for r in reqs), {}, snap) + "\n\nREQUIREMENTS:\n" + "\n".join(_req_block(r) for r in reqs))
    raw = llm.complete_json(system, user)
    out: dict = {}
    for it in raw.get("items", []) if isinstance(raw.get("items"), list) else []:
        try:
            rid = int(it.get("id"))
        except (TypeError, ValueError):
            continue
        cls = it.get("class") if it.get("class") in CLASSES else "configure"
        out[rid] = {"class": cls, "reason": str(it.get("reason", ""))[:300], "approach": str(it.get("approach", ""))[:300],
                    "question": str(it.get("question", ""))[:300]}
    return out


# --------------------------------------------------------------------------------------------------------- reviewer
def reviewer(reqs: list, cs, llm) -> dict:
    """Independent second opinion on coverage claims. Returns {id: {"verdict": pass|partial|fail, "issue": str}}."""
    system = ("You are a strict ERPNext reviewer. A developer proposes operations that claim to satisfy requirements. "
              "For EACH claimed requirement decide: pass (fully satisfied), partial (some of it missing or approximated), "
              "fail (does not satisfy). Judge against the requirement text only; be specific about what is missing. "
              'Return JSON: {"verdicts":[{"id":int,"verdict":"pass|partial|fail","issue":str}]}. JSON only.')
    ops = json.dumps([o.model_dump(mode="json", exclude_none=True) for o in cs.ops], ensure_ascii=False)[:9000]
    claimed = [r for r in reqs if r.id in set(cs.requirement_ids)]
    user = ("CLAIMED REQUIREMENTS:\n" + "\n".join(_req_block(r) for r in claimed) + "\n\nPROPOSED OPERATIONS (JSON):\n" + ops)
    raw = llm.complete_json(system, user)
    out: dict = {}
    for v in raw.get("verdicts", []) if isinstance(raw.get("verdicts"), list) else []:
        try:
            out[int(v.get("id"))] = {"verdict": v.get("verdict") if v.get("verdict") in ("pass", "partial", "fail") else "partial",
                                    "issue": str(v.get("issue", ""))[:400]}
        except (TypeError, ValueError):
            continue
    return out


# ---------------------------------------------------------------------------------------------------------- tester
def tester(reqs: list, cs, snap, llm) -> list:
    """Behavioural test cases (typed) for rules that can be exercised by saving a document."""
    system = ("You write behavioural tests for ERPNext changes. Each test saves ONE throwaway document through the API and states what must happen. "
              "Only write tests for validations/mandatory rules/new DocTypes that a save can exercise. Use real field names from the context. "
              "Prefer custom DocTypes created in the change set (you know every field). Provide ALL mandatory values.\n"
              "ERPNext facts: a mandatory Select field is auto-filled with its first option, so NEVER write a test that expects an error for leaving a Select empty; "
              "mandatory_depends_on (conditional mandatory) is NOT enforced when saving through the API unless a server script enforces it, so test it only if the change set has a server script for it.\n"
              'Return JSON: {"tests":[{"id":str,"requirement_id":int,"doctype":str,"kind":"insert_expect_error|insert_expect_ok",'
              '"values":{fieldname:value},"error_contains":str (for expect_error: a distinctive phrase of the expected message),"description":str}]}. '
              "At most 6 tests. JSON only.")
    ops = json.dumps([o.model_dump(mode="json", exclude_none=True) for o in cs.ops], ensure_ascii=False)[:8000]
    dts = [getattr(o, "doctype", None) or getattr(o, "name", None) for o in cs.ops]
    user = (build_context(" ".join(r.text for r in reqs), {}, snap, extra_doctypes=[d for d in dts if d]) + "\n\nREQUIREMENTS:\n"
            + "\n".join(_req_block(r) for r in reqs if r.id in set(cs.requirement_ids)) + "\n\nAPPLIED OPERATIONS (JSON):\n" + ops)
    raw = llm.complete_json(system, user)
    tests = raw.get("tests", [])
    return [t for t in tests if isinstance(t, dict)][:6]
