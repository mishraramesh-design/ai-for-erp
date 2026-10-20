"""Pure functions: typed operation -> the exact ERPNext document payloads. No network, no LLM."""
from __future__ import annotations

from .spec import DOC_EVENTS, slugify
from .validators import PROPERTY_TYPES, _norm_check_value


def b(v) -> int:
    return 1 if v else 0


def custom_field_payload(op) -> dict:
    d = {
        "doctype": "Custom Field", "dt": op.doctype, "fieldname": op.fieldname, "label": op.label,
        "fieldtype": op.fieldtype, "reqd": b(op.reqd), "in_list_view": b(op.in_list_view),
        "hidden": b(op.hidden), "read_only": b(op.read_only),
    }
    for k in ("insert_after", "options", "default", "description", "depends_on"):
        v = getattr(op, k)
        if v not in (None, ""):
            d[k] = v
    return d


def property_value(op) -> str:
    if PROPERTY_TYPES[op.property] == "Check":
        return _norm_check_value(op.value) or "0"
    return str(op.value)


def property_setter_payload(op) -> dict:
    d = {
        "doctype": "Property Setter",
        "doctype_or_field": "DocField" if op.fieldname else "DocType",
        "doc_type": op.doctype,
        "property": op.property,
        "value": property_value(op),
        "property_type": PROPERTY_TYPES[op.property],
    }
    if op.fieldname:
        d["field_name"] = op.fieldname
    return d


def script_name(doctype: str, name: str) -> str:
    base = name.strip()
    return base if base.lower().startswith(doctype.lower()) else f"{doctype} - {base}"


def client_script_payload(op) -> dict:
    return {"doctype": "Client Script", "name": script_name(op.doctype, op.name), "dt": op.doctype,
            "view": "Form", "enabled": 1, "script": op.script}


def server_script_payload(op) -> dict:
    return {"doctype": "Server Script", "name": script_name(op.doctype, op.name), "script_type": "DocType Event",
            "reference_doctype": op.doctype, "doctype_event": DOC_EVENTS[op.event], "script": op.script,
            "disabled": 0}


def _initials(name: str) -> str:
    parts = [p for p in name.replace("-", " ").split() if p]
    s = "".join(p[0] for p in parts).upper()
    return s if len(s) >= 2 else (name[:3].upper() or "DOC")


def _field_row(f, first_idx_flag: bool = False) -> dict:
    row = {
        "fieldname": f.fieldname, "label": f.label, "fieldtype": f.fieldtype, "reqd": b(f.reqd),
        "unique": b(f.unique), "in_list_view": b(f.in_list_view), "in_standard_filter": b(f.in_standard_filter),
        "read_only": b(f.read_only), "hidden": b(f.hidden),
    }
    for k in ("options", "default", "description", "depends_on", "fetch_from"):
        v = getattr(f, k)
        if v not in (None, ""):
            row[k] = v
    return row


def doctype_payload(op) -> dict:
    rows: list = []
    autoname = "hash"
    if op.naming == "series":
        series = f"{(op.series_prefix or _initials(op.name)).rstrip('-')}-.#####"
        rows.append({"fieldname": "naming_series", "label": "Series", "fieldtype": "Select", "options": series,
                     "default": series, "reqd": 1, "set_only_once": 1, "no_copy": 1, "print_hide": 1})
        autoname = "naming_series:"
    elif op.naming == "field":
        autoname = f"field:{op.name_field}"
    elif op.naming == "prompt":
        autoname = "Prompt"
    body = [_field_row(f) for f in op.fields]
    if not any(r["in_list_view"] for r in body):  # make the list view useful by default
        shown = 0
        for r, f in zip(body, op.fields):
            if f.fieldtype not in ("Section Break", "Column Break", "Tab Break", "HTML", "Table", "Text Editor",
                                   "Long Text", "Text", "Small Text") and shown < (4 if op.is_child_table else 3):
                r["in_list_view"] = 1
                shown += 1
    rows.extend(body)
    d = {
        "doctype": "DocType", "name": op.name, "module": op.module, "custom": 1,
        "istable": b(op.is_child_table), "issingle": 0, "is_submittable": b(op.is_submittable),
        "autoname": autoname, "track_changes": b(op.track_changes), "sort_field": "modified", "sort_order": "DESC",
        "fields": rows,
        "permissions": [] if op.is_child_table else [
            {"role": p.role, "permlevel": 0, **{k: b(getattr(p, k)) for k in (
                "read", "write", "create", "delete", "submit", "cancel", "amend", "report", "export", "print",
                "email", "share")}} for p in op.permissions
        ],
    }
    if op.is_child_table:
        d["editable_grid"] = 1
    if op.title_field:
        d["title_field"] = op.title_field
    if op.description:
        d["description"] = op.description
    return d


def workflow_payload(op) -> dict:
    return {
        "doctype": "Workflow", "workflow_name": op.name, "document_type": op.doctype, "is_active": 1,
        "workflow_state_field": "workflow_state", "override_status": 0, "send_email_alert": 0,
        "states": [{"state": s.state, "doc_status": str(s.doc_status), "allow_edit": s.allow_edit} for s in op.states],
        "transitions": [
            {"state": t.state, "action": t.action, "next_state": t.next_state, "allowed": t.allowed,
             "allow_self_approval": b(t.allow_self_approval), **({"condition": t.condition} if t.condition else {})}
            for t in op.transitions
        ],
    }


def desk_route(doctype: str) -> str:
    return "/app/" + slugify(doctype, limit=80).replace("_", "-")


# ------------------------------------------------------------------ recipe ops
def _filter_json(doctype: str, filters: list) -> str:
    import json
    return json.dumps([[doctype, f.field, f.operator, f.value, False] for f in filters])


def notification_payload(op) -> dict:
    rec = []
    for r in op.recipients:
        row = {}
        if r.field:
            row["receiver_by_document_field"] = r.field
        if r.role:
            row["receiver_by_role"] = r.role
        rec.append(row)
    d = {"doctype": "Notification", "name": op.name, "document_type": op.doctype, "event": op.event,
         "channel": op.channel, "enabled": 1, "message_type": "HTML", "message": op.message, "recipients": rec}
    if op.channel == "Email":
        d["subject"] = op.subject
    else:
        d.update({"subject": op.subject, "notification_type": "Alert", "notification_title": op.subject[:140]})
    if op.condition:
        d["condition"] = op.condition
    if op.event == "Value Change" and op.value_changed:
        d["value_changed"] = op.value_changed
    if op.event in ("Days Before", "Days After"):
        d["date_changed"] = op.date_field
        d["days_in_advance"] = op.days
    return d


def report_payload(op) -> dict:
    import json
    order = f"`tab{op.doctype}`.`{op.sort_by or 'modified'}` {'desc' if op.sort_desc else 'asc'}"
    cfg = {"filters": [[op.doctype, f.field, f.operator, f.value, False] for f in op.filters],
           "fields": [[c, op.doctype] for c in op.columns], "order_by": order, "add_totals_row": 0,
           "page_length": 20, "column_widths": {}, "group_by": None}
    return {"doctype": "Report", "name": op.name, "report_name": op.name, "ref_doctype": op.doctype,
            "report_type": "Report Builder", "is_standard": "No", "json": json.dumps(cfg),
            "roles": [{"role": r} for r in op.roles]}


def number_card_payload(op) -> dict:
    d = {"doctype": "Number Card", "name": op.label, "label": op.label, "type": "Document Type",
         "document_type": op.doctype, "function": op.function, "filters_json": _filter_json(op.doctype, op.filters),
         "show_percentage_stats": 1, "stats_time_interval": "Daily", "is_public": 1}
    if op.function != "Count" and op.field:
        d["aggregate_function_based_on"] = op.field
    if op.color:
        d["color"] = op.color
    return d


def dashboard_chart_payload(op) -> dict:
    d = {"doctype": "Dashboard Chart", "name": op.name, "chart_name": op.name, "document_type": op.doctype,
         "chart_type": op.chart_type, "type": op.graph, "filters_json": _filter_json(op.doctype, op.filters),
         "is_public": 1}
    if op.chart_type == "Group By":
        d.update({"group_by_type": "Count", "group_by_based_on": op.group_by_field, "number_of_groups": 10,
                  "timeseries": 0})
    else:
        d.update({"timeseries": 1, "based_on": op.date_field, "time_interval": op.interval, "timespan": op.timespan})
        if op.chart_type in ("Sum", "Average"):
            d["value_based_on"] = op.value_field
    return d


def dashboard_payload(op) -> dict:
    return {"doctype": "Dashboard", "name": op.name, "dashboard_name": op.name,
            "charts": [{"chart": c, "width": "Half"} for c in op.charts], "cards": [{"card": c} for c in op.cards]}


def html_block_payload(op) -> dict:
    return {"doctype": "Custom HTML Block", "name": op.name, "html": op.html, "style": op.style, "private": 0}


def workspace_payload(op) -> dict:
    import json
    blocks: list = []

    def add(kind: str, **data) -> None:
        blocks.append({"id": f"b{len(blocks) + 1:02d}", "type": kind, "data": data})

    add("header", text=f'<span class="h4"><b>{op.title}</b></span>', col=12)
    for n in op.html_blocks:
        add("custom_block", custom_block_name=n, col=12)
    for n in op.cards:
        add("number_card", number_card_name=n, col=4)
    if op.cards:
        add("spacer", col=12)
    for n in op.charts:
        add("chart", chart_name=n, col=6)
    if op.shortcuts:
        add("spacer", col=12)
        add("header", text='<span class="h5"><b>Shortcuts</b></span>', col=12)
        for sc in op.shortcuts:
            add("shortcut", shortcut_name=sc.label, col=3)
    d = {"doctype": "Workspace", "name": op.title, "label": op.title, "title": op.title, "public": 1, "module": op.module,
         "content": json.dumps(blocks),
         "custom_blocks": [{"custom_block_name": n, "label": n} for n in op.html_blocks],
         "number_cards": [{"number_card_name": n, "label": n} for n in op.cards],
         "charts": [{"chart_name": n, "label": n} for n in op.charts],
         "shortcuts": [{"type": "DocType", "link_to": sc.doctype, "label": sc.label, "doc_view": sc.view}
                       for sc in op.shortcuts],
         "roles": [{"role": r} for r in op.roles]}
    return d


def record_payload(op) -> dict:
    return {"create_notification": notification_payload, "create_report": report_payload,
            "create_number_card": number_card_payload, "create_dashboard_chart": dashboard_chart_payload,
            "create_dashboard": dashboard_payload, "create_html_block": html_block_payload,
            "create_workspace": workspace_payload}[op.op](op)
