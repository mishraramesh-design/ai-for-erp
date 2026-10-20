"""Deterministic validation and ordering of change sets. No LLM is involved here (Charter D6)."""
from __future__ import annotations

import ast
import difflib
import re
from dataclasses import asdict, dataclass
from typing import Optional

from .spec import FIELD_TYPES, LAYOUT_TYPES, ChangeSet

FIELDNAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,59}$")
DOCTYPE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9 _-]{1,60}$")
ROLE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9 _-]{1,60}$")
SCRIPT_NAME_RE = re.compile(r"^[A-Za-z0-9 _.\-]{3,120}$")

RESERVED_FIELDNAMES = {
    "name", "owner", "creation", "modified", "modified_by", "docstatus", "idx", "parent", "parenttype",
    "parentfield", "doctype", "amended_from", "naming_series", "workflow_state", "_comments", "_assign",
    "_liked_by", "_user_tags", "items", "meta", "flags",
}
RESERVED_EXCEPT_FOR_DOCTYPES = {"naming_series", "items"}  # allowed in a brand-new DocType's own fields

# Property Setter properties the agent may change, and how the value is stored.
PROPERTY_TYPES = {
    "reqd": "Check", "hidden": "Check", "read_only": "Check", "in_list_view": "Check",
    "in_standard_filter": "Check", "allow_on_submit": "Check", "bold": "Check", "print_hide": "Check",
    "unique": "Check", "label": "Data", "default": "Text", "options": "Text", "description": "Text",
    "depends_on": "Data", "mandatory_depends_on": "Code", "read_only_depends_on": "Code",
}
# Single DocTypes the configuration agent may edit. Extend deliberately; never wildcard.
SETTING_DOCTYPES = {
    "Selling Settings", "Buying Settings", "Stock Settings", "Accounts Settings", "HR Settings",
    "Manufacturing Settings", "CRM Settings", "Support Settings", "Projects Settings",
}
SETTING_FIELD_TYPES = {"Data", "Int", "Float", "Currency", "Percent", "Check", "Select", "Link", "Small Text",
                       "Text", "Date", "Duration"}

FORBIDDEN_PY_NAMES = {"exec", "eval", "compile", "open", "__import__", "globals", "locals", "vars", "getattr",
                      "setattr", "delattr", "input", "breakpoint", "exit", "quit"}
WARN_PY_CALLS = ("frappe.db.sql", "frappe.db.commit", "frappe.db.delete", "frappe.delete_doc", "frappe.db.rollback")
FORBIDDEN_JS = (
    (r"\beval\s*\(", "eval()"), (r"\bnew\s+Function\b", "new Function"), (r"\bdocument\.cookie\b", "document.cookie"),
    (r"\bXMLHttpRequest\b", "XMLHttpRequest"), (r"\bfetch\s*\(", "fetch()"), (r"\bimport\s*\(", "dynamic import"),
    (r"\bwindow\.open\b", "window.open"), (r"<\s*script", "script tag"), (r"\blocalStorage\b", "localStorage"),
    (r"\bsessionStorage\b", "sessionStorage"), (r"https?://", "external URL"),
)
STANDARD_DOC_ATTRS = {
    "name", "owner", "creation", "modified", "modified_by", "docstatus", "idx", "parent", "parenttype",
    "parentfield", "doctype", "flags", "meta", "amended_from",
}
STANDARD_DOC_METHODS = {
    "get", "as_dict", "is_new", "db_set", "append", "set", "update", "insert", "save", "submit", "cancel",
    "delete", "reload", "run_method", "get_title", "get_doc_before_save", "has_value_changed", "precision",
    "get_formatted", "is_submittable", "get_password", "add_comment", "notify_update", "get_latest",
}
PHASE = {"create_role": 0, "create_doctype": 1, "add_custom_field": 2, "set_property": 3, "add_client_script": 4,
         "add_server_validation": 4, "create_workflow": 5, "set_setting": 5, "add_permission": 6}


@dataclass
class Issue:
    level: str  # error | warning
    code: str
    message: str
    op_index: int = -1

    def to_dict(self) -> dict:
        return asdict(self)


def has_errors(issues: list) -> bool:
    return any(i.level == "error" for i in issues)


# ---------------------------------------------------------------- ordering
def _doctype_refs(op) -> set:
    refs = set()
    for f in op.fields:
        if f.fieldtype in ("Link", "Table", "Table MultiSelect") and f.options:
            refs.add(f.options)
    return refs


def order_ops(ops: list) -> tuple[list, list]:
    """Stable phase order, then dependency order for create_doctype. Returns (ordered_ops, cyclic_doctype_names)."""
    ordered = sorted(enumerate(ops), key=lambda p: (PHASE.get(p[1].op, 9), p[0]))
    ops = [o for _, o in ordered]
    creates = [o for o in ops if o.op == "create_doctype"]
    names = {o.name for o in creates}
    remaining = list(creates)
    done: list = []
    placed: set = set()
    progressed = True
    while remaining and progressed:
        progressed = False
        for o in list(remaining):
            deps = (_doctype_refs(o) & names) - {o.name}
            if deps <= placed:
                done.append(o)
                placed.add(o.name)
                remaining.remove(o)
                progressed = True
    cyclic = [o.name for o in remaining]
    done.extend(remaining)
    out, it = [], iter(done)
    for o in ops:
        out.append(next(it) if o.op == "create_doctype" else o)
    return out, cyclic


# ---------------------------------------------------------------- validation
class _Ctx:
    def __init__(self, snap):
        self.snap = snap
        self.planned_doctypes: dict = {}  # name -> {"child": bool, "fields": set, "submittable": bool}
        self.planned_fields: dict = {}  # doctype -> set(fieldnames)
        self.planned_roles: set = set()

    def doctype_exists(self, name: str) -> bool:
        return name in self.planned_doctypes or self.snap.has_doctype(name)

    def is_child(self, name: str) -> bool:
        if name in self.planned_doctypes:
            return self.planned_doctypes[name]["child"]
        m = self.snap.meta(name)
        return bool(m and m.get("istable"))

    def fields(self, name: str) -> set:
        out = set(self.planned_fields.get(name, set()))
        if name in self.planned_doctypes:
            out |= self.planned_doctypes[name]["fields"]
        else:
            out |= self.snap.fieldnames(name)
        return out

    def role_exists(self, role: str) -> bool:
        return role in self.planned_roles or role in self.snap.roles()


def _close(name: str, options) -> str:
    m = difflib.get_close_matches(name, list(options), n=3, cutoff=0.6)
    return f" Did you mean: {', '.join(m)}?" if m else ""


def check_python(script: str, doc_fields: Optional[set], doc_ok: bool) -> list:
    """Static checks for Server Scripts (they run in Frappe's restricted sandbox: no imports, no underscores)."""
    found: list = []
    if not script.strip():
        return [("error", "script_empty", "The script is empty.")]
    if len(script) > 20000:
        return [("error", "script_too_long", "Script is longer than 20,000 characters.")]
    try:
        tree = ast.parse(script)
    except SyntaxError as e:
        return [("error", "python_syntax", f"Python syntax error at line {e.lineno}: {e.msg}")]
    call_funcs = {id(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    for n in ast.walk(tree):
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            found.append(("error", "py_import", "Imports are not allowed in Server Scripts; use the `frappe` object."))
        elif isinstance(n, ast.Name) and n.id in FORBIDDEN_PY_NAMES:
            found.append(("error", "py_forbidden_name", f"`{n.id}` is not allowed in Server Scripts."))
        elif isinstance(n, ast.Attribute) and n.attr.startswith("_"):
            found.append(("error", "py_underscore_attr", f"Attribute `{n.attr}` starts with an underscore, which the sandbox blocks."))
        elif isinstance(n, ast.While):
            found.append(("warning", "py_while", "A `while` loop can hang a worker; prefer a bounded `for` loop."))
    src = script
    for call in WARN_PY_CALLS:
        if call in src:
            found.append(("warning", "py_risky_call", f"`{call}` is a risky call; make sure it is really needed."))
    if "frappe.throw" not in src and "frappe.msgprint" not in src and "doc." not in src:
        found.append(("warning", "py_no_effect", "The script never touches `doc` or calls frappe.throw/msgprint."))
    if doc_ok and doc_fields is not None:
        seen: set = set()
        for n in ast.walk(tree):
            if (isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "doc"
                    and id(n) not in call_funcs and n.attr not in seen):
                seen.add(n.attr)
                if n.attr not in doc_fields and n.attr not in STANDARD_DOC_ATTRS:
                    found.append(("error", "unknown_doc_field",
                                  f"`doc.{n.attr}` is not a field of this DocType, so the script would fail at runtime."))
        for n in ast.walk(tree):
            if (isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "doc"
                    and id(n) in call_funcs and n.attr not in STANDARD_DOC_METHODS):
                found.append(("warning", "unknown_doc_method", f"`doc.{n.attr}()` is not a known document method."))
    return found


def check_js(script: str, doctype: str) -> list:
    found: list = []
    if not script.strip():
        return [("error", "script_empty", "The script is empty.")]
    if len(script) > 20000:
        return [("error", "script_too_long", "Script is longer than 20,000 characters.")]
    for pat, label in FORBIDDEN_JS:
        if re.search(pat, script):
            found.append(("error", "js_forbidden", f"Client script uses {label}, which is not allowed."))
    if "frappe.ui.form.on(" not in script:
        found.append(("error", "js_no_handler", "Client script must register handlers with frappe.ui.form.on(...)."))
    elif doctype not in script:
        found.append(("warning", "js_doctype_mismatch", f'The script does not mention "{doctype}" in its handler.'))
    if script.count("{") != script.count("}") or script.count("(") != script.count(")"):
        found.append(("error", "js_unbalanced", "Brackets or braces are unbalanced."))
    found.append(("warning", "js_unchecked", "JavaScript is checked for safety patterns only, not fully parsed."))
    return found


def _norm_check_value(v) -> Optional[str]:
    if isinstance(v, bool):
        return "1" if v else "0"
    if str(v).strip().lower() in ("1", "true", "yes"):
        return "1"
    if str(v).strip().lower() in ("0", "false", "no"):
        return "0"
    return None


def validate(cs: ChangeSet, snap) -> list:
    issues: list = []
    ctx = _Ctx(snap)
    ordered, cyclic = order_ops(cs.ops)
    if [id(o) for o in ordered] != [id(o) for o in cs.ops]:
        issues.append(Issue("error", "not_ordered", "Operations are not in dependency order; call order_ops first."))
    for name in cyclic:
        issues.append(Issue("error", "doctype_cycle", f'"{name}" is part of a circular Link/Table dependency.'))
    if not cs.ops:
        issues.append(Issue("error", "empty", "The change set has no operations."))
    if len(cs.ops) > 60:
        issues.append(Issue("error", "too_many_ops", "More than 60 operations in one change set; split it by module."))

    def add(level, code, msg, i):
        issues.append(Issue(level, code, msg, i))

    for i, op in enumerate(cs.ops):
        t = op.op
        if t == "create_role":
            if not ROLE_RE.match(op.role_name):
                add("error", "role_name", f'Invalid role name "{op.role_name}".', i)
            if ctx.role_exists(op.role_name):
                add("warning", "role_exists", f'Role "{op.role_name}" already exists; it will be reused.', i)
            ctx.planned_roles.add(op.role_name)

        elif t == "create_doctype":
            if not DOCTYPE_RE.match(op.name) or op.name != op.name.strip():
                add("error", "doctype_name", f'Invalid DocType name "{op.name}".', i)
            if ctx.doctype_exists(op.name):
                add("error", "doctype_exists", f'DocType "{op.name}" already exists.', i)
            if op.module not in snap.modules() and op.module != "Custom":
                add("error", "module_missing", f'Module "{op.module}" does not exist. Use "Custom" or an existing module.', i)
            if not op.fields:
                add("error", "no_fields", f'DocType "{op.name}" has no fields.', i)
            seen: set = set()
            for f in op.fields:
                if not FIELDNAME_RE.match(f.fieldname):
                    add("error", "fieldname", f'Invalid fieldname "{f.fieldname}" in {op.name} (lowercase letters, digits, underscores).', i)
                if f.fieldname in RESERVED_FIELDNAMES - RESERVED_EXCEPT_FOR_DOCTYPES:
                    add("error", "fieldname_reserved", f'"{f.fieldname}" is a reserved fieldname (in {op.name}).', i)
                if f.fieldname in seen:
                    add("error", "fieldname_duplicate", f'Duplicate fieldname "{f.fieldname}" in {op.name}.', i)
                seen.add(f.fieldname)
                if f.fieldtype not in FIELD_TYPES:
                    add("error", "fieldtype", f'Unsupported fieldtype "{f.fieldtype}" for {op.name}.{f.fieldname}.', i)
                _check_options(f.fieldtype, f.options, f"{op.name}.{f.fieldname}", ctx, add, i)
                if f.fieldtype == "Table" and f.options and ctx.doctype_exists(f.options) and not ctx.is_child(f.options):
                    add("error", "table_not_child", f'"{f.options}" is not a child table DocType.', i)
            if op.is_child_table and op.is_submittable:
                add("error", "child_submittable", "A child table cannot be submittable.", i)
            if op.naming == "field":
                if not op.name_field or op.name_field not in seen:
                    add("error", "name_field", "naming='field' needs name_field to be one of the fields.", i)
            if op.title_field and op.title_field not in seen:
                add("error", "title_field", f'title_field "{op.title_field}" is not one of the fields.', i)
            if not op.is_child_table and not op.permissions:
                add("error", "no_permissions", f'"{op.name}" has no role permissions; nobody could open it.', i)
            for p in op.permissions:
                if not ctx.role_exists(p.role):
                    add("error", "role_missing", f'Role "{p.role}" does not exist.' + _close(p.role, snap.roles()), i)
            ctx.planned_doctypes[op.name] = {"child": op.is_child_table, "fields": seen | {"name"},
                                             "submittable": op.is_submittable}

        elif t == "add_custom_field":
            if not ctx.doctype_exists(op.doctype):
                add("error", "doctype_missing", f'DocType "{op.doctype}" does not exist.' + _close(op.doctype, snap.doctypes()), i)
                continue
            if not FIELDNAME_RE.match(op.fieldname):
                add("error", "fieldname", f'Invalid fieldname "{op.fieldname}".', i)
            if not op.fieldname.startswith("custom_"):
                add("error", "custom_prefix", 'Custom fields must start with "custom_" so they never clash with ERPNext upgrades.', i)
            if op.fieldname in RESERVED_FIELDNAMES:
                add("error", "fieldname_reserved", f'"{op.fieldname}" is a reserved fieldname.', i)
            if op.fieldtype not in FIELD_TYPES:
                add("error", "fieldtype", f'Unsupported fieldtype "{op.fieldtype}".', i)
            if op.fieldname in ctx.fields(op.doctype):
                add("error", "field_exists", f'{op.doctype} already has a field "{op.fieldname}".', i)
            if not op.label.strip() and op.fieldtype not in LAYOUT_TYPES:
                add("error", "label", "A label is required.", i)
            _check_options(op.fieldtype, op.options, f"{op.doctype}.{op.fieldname}", ctx, add, i)
            if op.insert_after and op.insert_after not in ctx.fields(op.doctype):
                add("warning", "insert_after_missing", f'"{op.insert_after}" is not a field of {op.doctype}; the field will go at the end.', i)
            if op.reqd and ctx.doctype_exists(op.doctype) and op.doctype not in ctx.planned_doctypes:
                add("warning", "reqd_existing", "Existing records without a value will fail validation when next saved.", i)
            ctx.planned_fields.setdefault(op.doctype, set()).add(op.fieldname)

        elif t == "set_property":
            if not ctx.doctype_exists(op.doctype):
                add("error", "doctype_missing", f'DocType "{op.doctype}" does not exist.' + _close(op.doctype, snap.doctypes()), i)
                continue
            if op.property not in PROPERTY_TYPES:
                add("error", "property", f'Property "{op.property}" is not allowed. Allowed: {", ".join(sorted(PROPERTY_TYPES))}.', i)
                continue
            if op.fieldname and op.fieldname not in ctx.fields(op.doctype):
                add("error", "field_missing", f'{op.doctype} has no field "{op.fieldname}".' + _close(op.fieldname, ctx.fields(op.doctype)), i)
            if PROPERTY_TYPES[op.property] == "Check" and _norm_check_value(op.value) is None:
                add("error", "property_value", f"{op.property} must be true/false (1/0).", i)
            if not op.fieldname and op.property not in ("label", "description"):
                add("warning", "doctype_level_property", f"{op.property} is normally a field-level property.", i)

        elif t == "add_client_script":
            if not ctx.doctype_exists(op.doctype):
                add("error", "doctype_missing", f'DocType "{op.doctype}" does not exist.' + _close(op.doctype, snap.doctypes()), i)
                continue
            if not SCRIPT_NAME_RE.match(op.name):
                add("error", "script_name", f'Invalid script name "{op.name}".', i)
            for lv, code, msg in check_js(op.script, op.doctype):
                add(lv, code, msg, i)
            if snap.doc_exists("Client Script", op.name):
                add("warning", "script_exists", f'Client Script "{op.name}" exists and will be overwritten (undo restores it).', i)

        elif t == "add_server_validation":
            if not ctx.doctype_exists(op.doctype):
                add("error", "doctype_missing", f'DocType "{op.doctype}" does not exist.' + _close(op.doctype, snap.doctypes()), i)
                continue
            if not SCRIPT_NAME_RE.match(op.name):
                add("error", "script_name", f'Invalid script name "{op.name}".', i)
            known = ctx.fields(op.doctype)
            for lv, code, msg in check_python(op.script, known, bool(known)):
                add(lv, code, msg, i)
            if snap.doc_exists("Server Script", op.name):
                add("warning", "script_exists", f'Server Script "{op.name}" exists and will be overwritten (undo restores it).', i)

        elif t == "set_setting":
            if op.doctype not in SETTING_DOCTYPES:
                add("error", "setting_not_allowed", f'"{op.doctype}" is not on the configuration allow-list: {", ".join(sorted(SETTING_DOCTYPES))}.', i)
                continue
            m = snap.meta(op.doctype)
            if not m or not m.get("issingle"):
                add("error", "not_single", f'"{op.doctype}" was not found as a settings DocType on this site.', i)
                continue
            f = next((x for x in m["fields"] if x.get("fieldname") == op.field), None)
            if not f:
                add("error", "field_missing", f'{op.doctype} has no field "{op.field}".' + _close(op.field, [x["fieldname"] for x in m["fields"] if x.get("fieldname")]), i)
                continue
            if f.get("fieldtype") not in SETTING_FIELD_TYPES:
                add("error", "setting_type", f'Field type {f.get("fieldtype")} cannot be set this way.', i)
            elif f["fieldtype"] == "Check" and _norm_check_value(op.value) is None:
                add("error", "setting_value", "A checkbox setting must be true/false.", i)
            elif f["fieldtype"] in ("Int", "Float", "Currency", "Percent"):
                try:
                    float(op.value)
                except (TypeError, ValueError):
                    add("error", "setting_value", f"{op.field} needs a number.", i)
            elif f["fieldtype"] == "Select" and f.get("options") and str(op.value) not in f["options"].split("\n"):
                add("error", "setting_value", f'Allowed values for {op.field}: {f["options"].replace(chr(10), ", ")}.', i)

        elif t == "create_workflow":
            _check_workflow(op, ctx, snap, add, i)

        elif t == "add_permission":
            if op.doctype in ctx.planned_doctypes:
                add("error", "perm_on_new_doctype", f'Put "{op.doctype}" permissions inside its create_doctype operation.', i)
                continue
            if not snap.has_doctype(op.doctype):
                add("error", "doctype_missing", f'DocType "{op.doctype}" does not exist.' + _close(op.doctype, snap.doctypes()), i)
                continue
            if not ctx.role_exists(op.role):
                add("error", "role_missing", f'Role "{op.role}" does not exist.' + _close(op.role, snap.roles()), i)
            if not 0 <= op.permlevel <= 9:
                add("error", "permlevel", "permlevel must be between 0 and 9.", i)
            if not any(getattr(op, k) for k in ("read", "write", "create", "delete", "submit", "cancel", "amend",
                                                 "report", "export", "print", "email", "share")):
                add("error", "no_rights", "At least one right must be granted.", i)
            add("warning", "perm_high_risk", "Permission changes affect who can see or edit data; review carefully.", i)
    return issues


def _check_workflow(op, ctx, snap, add, i) -> None:
    if not ctx.doctype_exists(op.doctype):
        add("error", "doctype_missing", f'DocType "{op.doctype}" does not exist.' + _close(op.doctype, snap.doctypes()), i)
        return
    if ctx.is_child(op.doctype):
        add("error", "workflow_child", "A workflow cannot be attached to a child table.", i)
    if not re.match(r"^[A-Za-z0-9 _.\-]{3,120}$", op.name):
        add("error", "workflow_name", f'Invalid workflow name "{op.name}".', i)
    if op.doctype in ctx.planned_doctypes:
        submittable = ctx.planned_doctypes[op.doctype]["submittable"]
    else:
        submittable = bool((snap.meta(op.doctype) or {}).get("is_submittable"))
    existing = snap.active_workflow(op.doctype) if op.doctype not in ctx.planned_doctypes else None
    if existing and existing != op.name:
        add("error", "workflow_active_exists",
            f'{op.doctype} already has the active workflow "{existing}"; activating another would switch it off.', i)
    if len(op.states) < 2:
        add("error", "workflow_states", "A workflow needs at least two states.", i)
    names = [s.state for s in op.states]
    if len(set(names)) != len(names):
        add("error", "workflow_state_dup", "State names must be unique.", i)
    status = {s.state: s.doc_status for s in op.states}
    for s in op.states:
        if not ctx.role_exists(s.allow_edit):
            add("error", "role_missing", f'Role "{s.allow_edit}" (state "{s.state}") does not exist.' + _close(s.allow_edit, snap.roles()), i)
        if s.doc_status != 0 and not submittable:
            add("error", "workflow_docstatus", f'State "{s.state}" uses doc status {s.doc_status}, but {op.doctype} is not submittable.', i)
    if op.states and op.states[0].doc_status != 0:
        add("error", "workflow_start", "The first state must be a draft (doc_status 0).", i)
    seen_pairs: set = set()
    for t in op.transitions:
        if t.state not in status or t.next_state not in status:
            add("error", "workflow_transition", f'Transition "{t.action}" refers to an unknown state ({t.state} -> {t.next_state}).', i)
            continue
        if not ctx.role_exists(t.allowed):
            add("error", "role_missing", f'Role "{t.allowed}" (action "{t.action}") does not exist.' + _close(t.allowed, snap.roles()), i)
        a, b = status[t.state], status[t.next_state]
        if (a == 1 and b == 0) or (a == 0 and b == 2) or (a == 2 and b != 2):
            add("error", "workflow_docstatus_flow", f'"{t.action}" moves doc status {a} to {b}, which ERPNext does not allow.', i)
        key = (t.state, t.action)
        if key in seen_pairs:
            add("error", "workflow_dup_action", f'Action "{t.action}" appears twice from state "{t.state}".', i)
        seen_pairs.add(key)
    reach = {op.states[0].state} if op.states else set()
    grew = True
    while grew:
        grew = False
        for t in op.transitions:
            if t.state in reach and t.next_state in status and t.next_state not in reach:
                reach.add(t.next_state)
                grew = True
    for n in names:
        if n not in reach:
            add("warning", "workflow_unreachable", f'State "{n}" can never be reached from "{names[0]}".', i)


def _check_options(fieldtype, options, where, ctx, add, i):
    if fieldtype == "Select" and not (options or "").strip():
        add("error", "select_options", f"{where}: a Select field needs options (one per line).", i)
    if fieldtype in ("Link", "Table", "Table MultiSelect"):
        if not options:
            add("error", "link_options", f"{where}: {fieldtype} needs the target DocType in options.", i)
        elif not ctx.doctype_exists(options):
            add("error", "link_target", f'{where}: target DocType "{options}" does not exist.' + _close(options, ctx.snap.doctypes()), i)
