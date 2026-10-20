"""Typed change-set spec.

The agent may only emit these operations; nothing else is ever applied (Charter D2).
Every operation can be described in plain words, scored for risk, and undone.
"""
from __future__ import annotations

import re
import secrets
import time
from typing import Annotated, Any, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

FIELD_TYPES = (
    "Data", "Int", "Float", "Currency", "Percent", "Check", "Date", "Datetime", "Time", "Select", "Link",
    "Dynamic Link", "Text", "Small Text", "Long Text", "Text Editor", "Attach", "Attach Image", "Phone",
    "Rating", "Duration", "Table", "Table MultiSelect", "Read Only", "Section Break", "Column Break",
    "Tab Break", "HTML",
)
LAYOUT_TYPES = ("Section Break", "Column Break", "Tab Break", "HTML")

DOC_EVENTS = {
    "before_insert": "Before Insert",
    "before_validate": "Before Validate",
    "before_save": "Before Save",
    "after_insert": "After Insert",
    "after_save": "After Save",
    "before_submit": "Before Submit",
    "after_submit": "After Submit",
    "before_cancel": "Before Cancel",
    "after_cancel": "After Cancel",
    "before_delete": "Before Delete",
    "after_delete": "After Delete",
}
EventName = Literal[
    "before_insert", "before_validate", "before_save", "after_insert", "after_save", "before_submit",
    "after_submit", "before_cancel", "after_cancel", "before_delete", "after_delete",
]

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_id() -> str:
    """26-character sortable id (time-ordered, ULID-style) used to name ledger files."""
    t = int(time.time() * 1000)
    ts = ""
    for _ in range(10):
        ts = _CROCKFORD[t % 32] + ts
        t //= 32
    return ts + "".join(secrets.choice(_CROCKFORD) for _ in range(16))


def slugify(text: str, prefix: str = "", limit: int = 60) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", (text or "").lower()).strip("_")
    return (prefix + s)[:limit].strip("_")


class _Op(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AddCustomField(_Op):
    op: Literal["add_custom_field"] = "add_custom_field"
    doctype: str
    fieldname: str
    label: str
    fieldtype: str
    insert_after: Optional[str] = None
    options: Optional[str] = None
    reqd: bool = False
    default: Optional[str] = None
    description: Optional[str] = None
    in_list_view: bool = False
    hidden: bool = False
    read_only: bool = False
    depends_on: Optional[str] = None


class SetProperty(_Op):
    op: Literal["set_property"] = "set_property"
    doctype: str
    fieldname: Optional[str] = None  # None = a DocType-level property
    property: str
    value: Any


class AddClientScript(_Op):
    op: Literal["add_client_script"] = "add_client_script"
    doctype: str
    name: str
    script: str


class AddServerValidation(_Op):
    op: Literal["add_server_validation"] = "add_server_validation"
    doctype: str
    event: EventName = "before_save"
    name: str
    script: str


class SetSetting(_Op):
    op: Literal["set_setting"] = "set_setting"
    doctype: str  # a Single DocType such as "Selling Settings"
    field: str
    value: Any


class CreateRole(_Op):
    op: Literal["create_role"] = "create_role"
    role_name: str


class AddPermission(_Op):
    op: Literal["add_permission"] = "add_permission"
    doctype: str
    role: str
    permlevel: int = 0
    read: bool = True
    write: bool = False
    create: bool = False
    delete: bool = False
    submit: bool = False
    cancel: bool = False
    amend: bool = False
    report: bool = False
    export: bool = False
    print: bool = False
    email: bool = False
    share: bool = False


class DocFieldSpec(_Op):
    fieldname: str
    label: str
    fieldtype: str
    options: Optional[str] = None
    reqd: bool = False
    unique: bool = False
    in_list_view: bool = False
    in_standard_filter: bool = False
    default: Optional[str] = None
    description: Optional[str] = None
    depends_on: Optional[str] = None
    read_only: bool = False
    hidden: bool = False
    fetch_from: Optional[str] = None


class PermSpec(_Op):
    role: str
    read: bool = True
    write: bool = False
    create: bool = False
    delete: bool = False
    submit: bool = False
    cancel: bool = False
    amend: bool = False
    report: bool = True
    export: bool = False
    print: bool = True
    email: bool = False
    share: bool = False


class CreateDocType(_Op):
    op: Literal["create_doctype"] = "create_doctype"
    name: str
    module: str = "Custom"
    description: Optional[str] = None
    is_child_table: bool = False
    is_submittable: bool = False
    naming: Literal["series", "field", "hash", "prompt"] = "series"
    series_prefix: Optional[str] = None
    name_field: Optional[str] = None
    title_field: Optional[str] = None
    track_changes: bool = True
    fields: list[DocFieldSpec]
    permissions: list[PermSpec] = Field(default_factory=list)


class WorkflowStateSpec(_Op):
    state: str
    doc_status: Literal[0, 1, 2] = 0  # 0 draft, 1 submitted, 2 cancelled
    allow_edit: str  # role that may edit while in this state


class WorkflowTransitionSpec(_Op):
    state: str
    action: str
    next_state: str
    allowed: str  # role that may take this action
    allow_self_approval: bool = True
    condition: Optional[str] = None


class CreateWorkflow(_Op):
    op: Literal["create_workflow"] = "create_workflow"
    name: str
    doctype: str
    states: list[WorkflowStateSpec]
    transitions: list[WorkflowTransitionSpec]


# ------------------------------------------------------------------ recipe ops (notifications, reports, dashboards)
class NotifyRecipient(_Op):
    field: Optional[str] = None  # a field of the document holding an email address / user
    role: Optional[str] = None   # everyone with this role


class CreateNotification(_Op):
    op: Literal["create_notification"] = "create_notification"
    name: str
    doctype: str
    event: Literal["New", "Save", "Submit", "Cancel", "Value Change", "Days Before", "Days After"]
    subject: str
    message: str  # HTML with Jinja, e.g. {{ doc.supplier_name }}
    channel: Literal["Email", "System Notification"] = "Email"
    condition: Optional[str] = None  # Python expression on doc, e.g. doc.status == "Approved"
    value_changed: Optional[str] = None  # field, for event "Value Change"
    date_field: Optional[str] = None     # field, for "Days Before"/"Days After"
    days: int = 0
    recipients: list[NotifyRecipient] = Field(default_factory=list)


class ReportFilter(_Op):
    field: str
    operator: Literal["=", "!=", ">", "<", ">=", "<=", "like", "in", "not in"] = "="
    value: Any = None


class CreateReport(_Op):
    op: Literal["create_report"] = "create_report"
    name: str
    doctype: str
    columns: list[str]
    filters: list[ReportFilter] = Field(default_factory=list)
    sort_by: Optional[str] = None
    sort_desc: bool = True
    roles: list[str] = Field(default_factory=list)  # empty = the DocType's own permissions decide


class CreateNumberCard(_Op):
    op: Literal["create_number_card"] = "create_number_card"
    label: str
    doctype: str
    function: Literal["Count", "Sum", "Average", "Minimum", "Maximum"] = "Count"
    field: Optional[str] = None  # numeric field for Sum/Average/Minimum/Maximum
    filters: list[ReportFilter] = Field(default_factory=list)
    color: Optional[str] = None


class CreateDashboardChart(_Op):
    op: Literal["create_dashboard_chart"] = "create_dashboard_chart"
    name: str
    doctype: str
    chart_type: Literal["Count", "Sum", "Average", "Group By"] = "Group By"
    group_by_field: Optional[str] = None  # Group By: the field whose values become the bars
    date_field: Optional[str] = None      # Count/Sum/Average: the date field of the time axis
    value_field: Optional[str] = None     # Sum/Average: numeric field
    graph: Literal["Line", "Bar", "Pie", "Donut", "Percentage"] = "Bar"
    interval: Literal["Yearly", "Quarterly", "Monthly", "Weekly", "Daily"] = "Monthly"
    timespan: Literal["Last Year", "Last Quarter", "Last Month", "Last Week"] = "Last Year"
    filters: list[ReportFilter] = Field(default_factory=list)


class CreateDashboard(_Op):
    op: Literal["create_dashboard"] = "create_dashboard"
    name: str
    charts: list[str] = Field(default_factory=list)  # names of dashboard charts
    cards: list[str] = Field(default_factory=list)   # labels of number cards


RECORD_OPS = {"create_notification": "Notification", "create_report": "Report", "create_number_card": "Number Card",
              "create_dashboard_chart": "Dashboard Chart", "create_dashboard": "Dashboard"}


Op = Annotated[
    Union[
        AddCustomField, SetProperty, AddClientScript, AddServerValidation, SetSetting,
        CreateRole, AddPermission, CreateDocType, CreateWorkflow, CreateNotification, CreateReport,
        CreateNumberCard, CreateDashboardChart, CreateDashboard,
    ],
    Field(discriminator="op"),
]
OpAdapter = TypeAdapter(Op)

RISK = {
    "add_custom_field": 0, "create_doctype": 0, "create_role": 0,
    "set_property": 1, "add_client_script": 1, "add_server_validation": 1, "set_setting": 1,
    "add_permission": 2, "create_workflow": 2,
    "create_notification": 1, "create_report": 0, "create_number_card": 0, "create_dashboard_chart": 0, "create_dashboard": 0,
}
RISK_LEVELS = ("low", "medium", "high")


def risk_of(ops: list) -> str:
    m = max((RISK[o.op] for o in ops), default=0)
    if len(ops) > 15:
        m = max(m, 1)
    return RISK_LEVELS[m]


def describe(op) -> str:
    t = op.op
    if t == "add_custom_field":
        s = f'Add {op.fieldtype} field "{op.label}" ({op.fieldname}) to {op.doctype}'
        if op.insert_after:
            s += f" after {op.insert_after}"
        if op.reqd:
            s += ", mandatory"
        if op.options:
            s += ", options: " + op.options.replace("\n", " / ")
        return s
    if t == "set_property":
        target = f'field "{op.fieldname}" of {op.doctype}' if op.fieldname else f"{op.doctype} (form level)"
        return f"Set {op.property} = {op.value} on {target}"
    if t == "add_client_script":
        return f'Add client script "{op.name}" to the {op.doctype} form'
    if t == "add_server_validation":
        return f'Add server script "{op.name}" on {op.doctype} ({op.event.replace("_", " ")})'
    if t == "set_setting":
        return f"Set {op.doctype} › {op.field} = {op.value}"
    if t == "create_role":
        return f'Create role "{op.role_name}"'
    if t == "add_permission":
        rights = [k for k in ("read", "write", "create", "delete", "submit", "cancel", "amend", "report",
                              "export", "print", "email", "share") if getattr(op, k)]
        return f'Grant role "{op.role}" on {op.doctype} (level {op.permlevel}): {", ".join(rights) or "nothing"}'
    if t == "create_workflow":
        return (f'Create workflow "{op.name}" on {op.doctype}: {len(op.states)} states, '
                f'{len(op.transitions)} transitions')
    if t == "create_notification":
        return f'Create {op.channel.lower()} notification "{op.name}" on {op.doctype} ({op.event})'
    if t == "create_report":
        return f'Create report "{op.name}" on {op.doctype} with {len(op.columns)} columns'
    if t == "create_number_card":
        return f'Create number card "{op.label}": {op.function}{" of " + op.field if op.field else ""} on {op.doctype}'
    if t == "create_dashboard_chart":
        return f'Create {op.graph.lower()} chart "{op.name}" on {op.doctype}'
    if t == "create_dashboard":
        return f'Create dashboard "{op.name}" with {len(op.charts)} chart(s) and {len(op.cards)} number card(s)'
    if t == "create_doctype":
        kind = "child table" if op.is_child_table else "DocType"
        s = f'Create {kind} "{op.name}" in module {op.module} with {len(op.fields)} fields'
        if op.permissions:
            s += f" and {len(op.permissions)} role permission(s)"
        return s
    return t


class ChangeSet(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str = Field(default_factory=new_id)
    title: str = ""
    intent: str = ""
    summary: str = ""
    source: Literal["rule", "pattern", "replicate", "llm", "plan"] = "llm"
    ops: list[Op]
    status: Literal["proposed", "applying", "applied", "failed", "undoing", "undone", "discarded"] = "proposed"
    created_at: float = Field(default_factory=time.time)
    created_by: str = ""
    session_id: str = ""
    pattern_id: Optional[str] = None
    promoted_pattern: Optional[str] = None
    requirement_ids: list[int] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    questions: list[str] = Field(default_factory=list)
    not_covered: list[dict] = Field(default_factory=list)
    llm_calls: int = 0
    warnings: list[str] = Field(default_factory=list)
    issues: list[dict] = Field(default_factory=list)
    steps: list[dict] = Field(default_factory=list)
    progress: dict = Field(default_factory=lambda: {"done": 0, "total": 0})
    error: str = ""

    def risk(self) -> str:
        return risk_of(self.ops)

    def to_public(self) -> dict:
        d = self.model_dump(mode="json", exclude={"steps"})
        d["risk"] = self.risk()
        d["lines"] = [describe(o) for o in self.ops]
        d["steps"] = [
            {k: v for k, v in s.items() if k in ("i", "op", "status", "note", "error")} for s in self.steps
        ]
        return d


class LlmProposal(BaseModel):
    """What the language model returns: either an answer, or an answer plus operations."""
    model_config = ConfigDict(extra="ignore")
    reply: str = ""
    title: str = ""
    summary: str = ""
    ops: list[Op] = Field(default_factory=list)
    questions: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    covered: list[int] = Field(default_factory=list)
    not_covered: list[dict] = Field(default_factory=list)
