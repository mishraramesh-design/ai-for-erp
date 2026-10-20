"""Whitelisted calls the Studio makes as Administrator while preparing a development site."""
import frappe

from studio_bridge.setup import SERVICE_USER, ensure_service_user


@frappe.whitelist()
def provision_service_user() -> dict:
    if frappe.session.user != "Administrator":
        frappe.throw("Only Administrator may provision the Studio service user", frappe.PermissionError)
    if not frappe.conf.get("developer_mode"):
        frappe.throw("Refusing to provision: this site is not in developer mode (the Studio never writes to production)")
    key, secret = ensure_service_user()
    return {"user": SERVICE_USER, "api_key": key, "api_secret": secret}
