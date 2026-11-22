"""Serve the CSD desk theme through the Python app (no static-asset linking needed).

The CSS comes from the single DocType "CSD Desk Theme" when it is enabled and non-empty, so the Studio can
change the look with a normal set_setting operation. Otherwise the bundled file is served."""
import os
import frappe


def _bundled():
    path = os.path.join(os.path.dirname(__file__), "public", "css", "csd_theme.css")
    with open(path, "rb") as f:
        return f.read()


def _from_site():
    try:
        if frappe.db.get_single_value("CSD Desk Theme", "enabled"):
            css = frappe.db.get_single_value("CSD Desk Theme", "css")
            if css and css.strip():
                return css.encode("utf-8")
    except Exception:
        pass
    return None


@frappe.whitelist(allow_guest=True)
def css():
    data = _from_site() or _bundled()
    frappe.local.response.filename = "csd_theme.css"
    frappe.local.response.filecontent = data
    frappe.local.response.type = "download"
    frappe.local.response.display_content_as = "inline"
