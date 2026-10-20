import json

import httpx
import pytest

from studio.prepare import prepare


def make(handler):
    return httpx.Client(base_url="http://erp", transport=httpx.MockTransport(handler))


def run(handler, **cfg):
    log = []
    c = {"url": "http://erp", "site": "erp.local", "admin_password": "pw", **cfg}
    try:
        out = prepare(c, lambda k, s, n: log.append((k, s, n)), client=make(handler), wait_s=0)
    except RuntimeError:
        out = None
    return out, log


def ok(body):
    return httpx.Response(200, json=body)


def test_happy_path_runs_wizard_once_when_no_company(monkeypatch):
    calls = []

    def h(req):
        calls.append(req.url.path)
        if req.url.path == "/api/method/ping":
            return ok({"message": "pong"})
        if req.url.path == "/api/resource/Company":
            return ok({"data": []})
        if req.url.path.endswith("setup_complete"):
            return ok({"message": {}})
        if req.url.path == "/api/method/studio_bridge.api.provision_service_user":
            return ok({"message": {"user": "s@x", "api_key": "k", "api_secret": "s"}})
        return ok({})
    monkeypatch.setattr("studio.prepare.HttpErp.get_meta", lambda self, dt: {"fields": [1, 2]})
    out, log = run(h, company="Acme Ltd")
    assert out == {"api_key": "k", "api_secret": "s"}
    assert [k for k, s, _ in log if s == "ok"] == ["reach", "login", "wizard", "service", "verify"]
    assert sum(p.endswith("setup_complete") for p in calls) == 1


def test_wrong_admin_password_is_plain():
    def h(req):
        if req.url.path == "/api/method/ping":
            return ok({"message": "pong"})
        return httpx.Response(401, json={})
    out, log = run(h)
    assert out is None and log[-1][:2] == ("login", "fail") and "password" in log[-1][2]


def test_unreachable_erp_reports_clearly():
    def h(req):
        raise httpx.ConnectError("no route")
    out, log = run(h)
    assert out is None and log[-1][:2] == ("reach", "fail")


def test_not_developer_mode_is_refused_in_words():
    def h(req):
        if req.url.path == "/api/method/ping":
            return ok({"message": "pong"})
        if req.url.path == "/api/resource/Company":
            return ok({"data": [{"name": "A"}]})
        if "provision_service_user" in req.url.path:
            return httpx.Response(417, json={"exception": "x", "_server_messages": json.dumps([json.dumps({"message": "Refusing: this site is not in developer mode"})])})
        return ok({})
    out, log = run(h)
    assert out is None and "developer mode" in log[-1][2]
