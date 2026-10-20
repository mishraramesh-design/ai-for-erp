"""Service-user provisioning (development sites only).

The Studio signs in as a dedicated service user so every change shows 'Studio Agent' in the audit trail.
`ensure_service_user()` creates it (idempotent) and issues a fresh API key and secret.
"""
import frappe

SERVICE_USER = "studio.agent@example.com"


def ensure_service_user() -> tuple[str, str]:
    if not frappe.db.exists("Role", "Studio User"):
        frappe.get_doc({"doctype": "Role", "role_name": "Studio User", "desk_access": 1}).insert(ignore_permissions=True)
    if frappe.db.exists("User", SERVICE_USER):
        user = frappe.get_doc("User", SERVICE_USER)
    else:
        user = frappe.get_doc({"doctype": "User", "email": SERVICE_USER, "first_name": "Studio Agent",
                               "send_welcome_email": 0, "user_type": "System User", "enabled": 1})
        user.insert(ignore_permissions=True)
    # The service user holds every assignable role so it can test the behaviour it builds (insert test records,
    # read new tables). Roles the Studio creates later are granted to it by the applier.
    auto = {"Guest", "All", "Administrator", "Desk User"}
    have = {r.role for r in user.roles}
    for role in frappe.get_all("Role", pluck="name"):
        if role not in auto and role not in have:
            user.append("roles", {"role": role})
    user.save(ignore_permissions=True)
    from frappe.core.doctype.user.user import generate_keys
    secret = generate_keys(SERVICE_USER)["api_secret"]
    frappe.db.commit()
    return frappe.db.get_value("User", SERVICE_USER, "api_key"), secret
