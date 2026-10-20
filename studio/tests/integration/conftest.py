"""Integration tests run against a real ERPNext site. Skipped unless STUDIO_IT_ERP_URL is set.

  STUDIO_IT_ERP_URL=http://127.0.0.1:18000 STUDIO_IT_HOST=studio.local STUDIO_IT_ADMIN_PASSWORD=admin pytest tests/integration
"""
import os
import uuid

import httpx
import pytest

from studio.erp import HttpErp

URL = os.environ.get("STUDIO_IT_ERP_URL")
HOST = os.environ.get("STUDIO_IT_HOST")
ADMIN_PW = os.environ.get("STUDIO_IT_ADMIN_PASSWORD", "admin")
pytestmark = pytest.mark.skipif(not URL, reason="STUDIO_IT_ERP_URL not set")


def pytest_collection_modifyitems(config, items):
    if URL:
        return
    skip = pytest.mark.skip(reason="STUDIO_IT_ERP_URL not set")
    for item in items:
        if "integration" in str(item.fspath):
            item.add_marker(skip)


@pytest.fixture(scope="session")
def erp():
    headers = {"Host": HOST} if HOST else {}
    admin = httpx.Client(base_url=URL, headers=headers, timeout=120)
    r = admin.post("/api/method/login", data={"usr": "Administrator", "pwd": ADMIN_PW})
    assert r.status_code == 200, r.text
    email = f"it.{uuid.uuid4().hex[:6]}@example.com"
    auto = {"Guest", "All", "Administrator", "Desk User"}
    roles = [x["name"] for x in admin.get("/api/resource/Role", params={"limit_page_length": 0}).json()["data"] if x["name"] not in auto]
    r = admin.post("/api/resource/User", json={"email": email, "first_name": "IT Agent", "send_welcome_email": 0,
                   "roles": [{"role": x} for x in roles]})
    assert r.status_code < 300, r.text
    r = admin.post("/api/method/frappe.core.doctype.user.user.generate_keys", data={"user": email})
    assert r.status_code == 200, r.text
    secret = r.json()["message"]["api_secret"]
    key = admin.get(f"/api/resource/User/{email}").json()["data"]["api_key"]
    yield HttpErp(URL, key, secret, host_header=HOST)
    admin.delete(f"/api/resource/User/{email}")
