"""Provisioning, run once after install:  bench --site <site> execute studio_bridge.setup.provision

Creates the dedicated service user the agent service signs in as (so every change shows 'Studio Agent' as its author
in the audit trail) and the 'Studio User' role. Secrets come from the environment, never from source.
"""
import os

import frappe

SERVICE_USER = "studio.agent@example.com"


def provision():
    key = os.environ.get("STUDIO_SVC_KEY")
    secret = os.environ.get("STUDIO_SVC_SECRET")
    if not key or not secret:
        raise SystemExit("STUDIO_SVC_KEY and STUDIO_SVC_SECRET must be set")
    if not frappe.db.exists("Role", "Studio User"):
        frappe.get_doc({"doctype": "Role", "role_name": "Studio User", "desk_access": 1}).insert(ignore_permissions=True)
    if frappe.db.exists("User", SERVICE_USER):
        user = frappe.get_doc("User", SERVICE_USER)
    else:
        user = frappe.get_doc({"doctype": "User", "email": SERVICE_USER, "first_name": "Studio Agent",
                               "send_welcome_email": 0, "user_type": "System User", "enabled": 1})
        user.insert(ignore_permissions=True)
    have = {r.role for r in user.roles}
    for role in ("System Manager",):  # the applier creates DocTypes, fields, scripts and workflows
        if role not in have:
            user.append("roles", {"role": role})
    user.api_key = key
    user.api_secret = secret
    user.save(ignore_permissions=True)
    frappe.db.commit()
    print(f"Service user ready: {SERVICE_USER}")
