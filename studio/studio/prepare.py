"""Prepare a vanilla ERPNext development site for the Studio, using only ERPNext's own HTTP API.

Steps: reach the site, sign in as Administrator, run the setup wizard if the site has no company yet,
create the 'Studio Agent' service user (refuses unless the site is in developer mode), verify the keys.
The compose file creates the site; nothing here needs SSH or Docker access.
"""
from __future__ import annotations

import time
from typing import Callable

import httpx

from .erp import HttpErp

Step = Callable[[str, str, str], None]  # (key, status, note)

STEPS = [
    ("reach", "Reach ERPNext"),
    ("login", "Sign in as Administrator"),
    ("wizard", "Complete the setup wizard"),
    ("service", "Create the Studio Agent user"),
    ("verify", "Verify the connection"),
]


def wizard_args(cfg: dict) -> dict:
    return {
        "language": "English", "country": cfg.get("country", "India"), "timezone": cfg.get("timezone", "Asia/Kolkata"),
        "currency": cfg.get("currency", "INR"), "company_name": cfg.get("company", "Demo Company"),
        "company_abbr": "".join(w[0] for w in cfg.get("company", "Demo Company").split()).upper()[:5] or "DC",
        "fy_start_date": cfg.get("fy_start", "2026-04-01"), "fy_end_date": cfg.get("fy_end", "2027-03-31"),
        "chart_of_accounts": "Standard", "full_name": "Studio Admin", "email": "studio.admin@example.com",
        "password": cfg["admin_password"],
    }


def prepare(cfg: dict, step: Step, client: httpx.Client | None = None, wait_s: int = 180) -> dict:
    """Returns {'api_key','api_secret'} on success. Raises RuntimeError with a plain-words message on failure."""
    c = client or httpx.Client(base_url=cfg["url"].rstrip("/"), headers={"Host": cfg["site"]}, timeout=400)

    step("reach", "run", "")
    deadline = time.time() + wait_s
    while True:
        try:
            if c.get("/api/method/ping").status_code == 200:
                break
        except httpx.HTTPError:
            pass
        if time.time() > deadline:
            step("reach", "fail", "No answer from ERPNext. Is the ERPNext project running and is the URL right?")
            raise RuntimeError("ERPNext did not answer")
        time.sleep(3)
    step("reach", "ok", cfg["url"])

    step("login", "run", "")
    r = c.post("/api/method/login", data={"usr": "Administrator", "pwd": cfg["admin_password"]})
    if r.status_code != 200:
        step("login", "fail", "Administrator password was rejected." if r.status_code in (401, 403) else f"HTTP {r.status_code}")
        raise RuntimeError("login failed")
    step("login", "ok", "")

    step("wizard", "run", "")
    companies = c.get("/api/resource/Company", params={"limit_page_length": 1}).json().get("data", [])
    if companies:
        step("wizard", "ok", f"Already done ({companies[0]['name']})")
    else:
        r = c.post("/api/method/frappe.desk.page.setup_wizard.setup_wizard.setup_complete",
                   data={"args": __import__("json").dumps(wizard_args(cfg))})
        if r.status_code != 200:
            step("wizard", "fail", r.text[:300])
            raise RuntimeError("setup wizard failed")
        # the wizard signs the session out; sign in again
        c.post("/api/method/login", data={"usr": "Administrator", "pwd": cfg["admin_password"]})
        step("wizard", "ok", f"Created company {cfg.get('company', 'Demo Company')}")

    step("service", "run", "")
    r = c.post("/api/method/studio_bridge.api.provision_service_user")
    if r.status_code != 200:
        from .erp import _detail
        note = _detail(r)[:300]
        low = r.text.lower()
        if "developer mode" in low:
            note = "This site is not in developer mode. Studio only works on a development site."
        elif "not installed" in low or r.status_code == 404:
            note = "The studio_bridge app is not installed on this site. Use the erp image from the Studio's compose file."
        step("service", "fail", note)
        raise RuntimeError("service user failed")
    svc = r.json()["message"]
    step("service", "ok", svc["user"] + (" · Server Scripts switched on" if svc.get("server_scripts_enabled_now") else ""))

    step("verify", "run", "")
    try:
        meta = HttpErp(cfg["url"], svc["api_key"], svc["api_secret"], host_header=cfg["site"]).get_meta("Customer")
    except Exception as e:  # reported, not raised
        step("verify", "fail", str(e)[:300])
        raise RuntimeError("verification failed") from e
    if not meta:
        step("verify", "fail", "Could not read the Customer DocType as the service user")
        raise RuntimeError("verification failed")
    step("verify", "ok", f"{len(meta['fields'])} Customer fields readable")
    return {"api_key": svc["api_key"], "api_secret": svc["api_secret"]}
