"""Serve the CSD desk theme through the Python app, so it works without any static-asset linking."""
import os
import frappe


@frappe.whitelist(allow_guest=True)
def css():
    path = os.path.join(os.path.dirname(__file__), "public", "css", "csd_theme.css")
    with open(path, "rb") as f:
        data = f.read()
    frappe.local.response.filename = "csd_theme.css"
    frappe.local.response.filecontent = data
    frappe.local.response.type = "download"
    frappe.local.response.display_content_as = "inline"
    frappe.local.response.headers = {"Content-Type": "text/css; charset=utf-8", "Cache-Control": "public, max-age=300"}
