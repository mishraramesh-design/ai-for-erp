import pytest
from fastapi.testclient import TestClient

from studio.app import create_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("STUDIO_ADMIN_PASSWORD", "pw123")
    return TestClient(create_app(str(tmp_path)))


def login(c):
    assert c.post("/api/login", json={"password": "pw123"}).status_code == 200


def test_requires_login(client):
    assert client.get("/api/status").status_code == 401
    assert client.get("/", follow_redirects=False).status_code == 303
    assert client.get("/health").json()["ok"]
    assert client.post("/api/login", json={"password": "bad"}).status_code == 401


def test_lockout_after_failures(client):
    for _ in range(5):
        client.post("/api/login", json={"password": "bad"})
    assert client.post("/api/login", json={"password": "pw123"}).status_code == 429


def test_model_key_encrypted_and_never_returned(client, tmp_path):
    login(client)
    r = client.put("/api/models", json={"api_key": "sk-secret-123", "cheap": "m-small", "strong": "m-big", "profile": "balanced"}).json()
    assert r["key_set"] and "sk-secret-123" not in str(r)
    assert r["roles"]["developer"]["primary"] == "m-small" and r["roles"]["developer"]["fallback"] == "m-big"
    assert "sk-secret-123" not in (tmp_path / "models.json").read_text()
    client.put("/api/models", json={"api_key": "", "cheap": "m-small2", "strong": "m-big"})  # blank keeps the stored key
    assert client.get("/api/models").json()["key_set"]
    assert client.get("/api/status").json()["llm"] is True
    # a new process with the same data directory still reads the key
    c2 = TestClient(create_app(str(tmp_path)))
    login(c2)
    assert c2.get("/api/models").json()["key_set"]


def test_models_test_needs_key(client):
    login(client)
    assert client.post("/api/models/test").json()["ok"] is False


def test_erp_password_encrypted_and_prepare_stores_keys(client, tmp_path, monkeypatch):
    import time
    from studio import app as appmod
    login(client)
    r = client.put("/api/erp", json={"admin_password": "adm-secret", "site": "erp.local"}).json()
    assert r["password_set"] and not r["ready"] and "adm-secret" not in str(r)
    assert "adm-secret" not in (tmp_path / "settings.json").read_text()

    seen = {}

    def fake_prepare(cfg, step):
        seen["pw"] = cfg["admin_password"]
        step("reach", "ok", "x")
        step("login", "ok", "")
        return {"api_key": "K1", "api_secret": "S1-secret"}

    monkeypatch.setattr(appmod, "prepare", fake_prepare)
    assert client.post("/api/erp/prepare").status_code == 200
    for _ in range(50):
        e = client.get("/api/erp").json()
        if not e["running"]:
            break
        time.sleep(0.05)
    assert seen["pw"] == "adm-secret" and e["ready"] and [x["key"] for x in e["steps"]] == ["reach", "login"]
    assert "S1-secret" not in (tmp_path / "settings.json").read_text()
    assert client.get("/api/status").json()["erp"] is True
    # changing the target clears the stored keys
    assert client.put("/api/erp", json={"url": "http://other:8000"}).json()["ready"] is False


def test_prepare_needs_password(client):
    login(client)
    assert client.post("/api/erp/prepare").status_code == 400


def test_pages_after_login(client):
    login(client)
    for p in ("/", "/workbench"):
        assert client.get(p).status_code == 200


def test_erp_defaults_seeded_from_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("STUDIO_ADMIN_PASSWORD", "pw123")
    monkeypatch.setenv("ERP_ADMIN_PASSWORD", "from-compose")
    monkeypatch.setenv("ERP_SITE", "erp.local")
    c = TestClient(create_app(str(tmp_path)))
    login(c)
    e = c.get("/api/erp").json()
    assert e["password_set"] and e["url"] == "http://backend:8000" and e["site"] == "erp.local"
    assert "from-compose" not in (tmp_path / "settings.json").read_text()


def test_requirements_import_and_run_guards(client):
    login(client)
    r = client.post("/api/requirements/import", files={"file": ("fr.txt", b"1. Record the date of every supplier site visit.\n2. Block POs above the limit.", "text/plain")},
                    data={"module": "SRM"}).json()
    assert r["added"] == 2
    assert client.post("/api/requirements/import", files={"file": ("fr.txt", b"x", "text/plain")}).status_code == 400  # module required
    items = client.get("/api/requirements?module=SRM").json()
    assert items["total"] == 2
    ids = [i["id"] for i in items["items"]]
    assert client.post("/api/runs", json={"ids": ids}).status_code == 409  # no model yet
    client.put("/api/models", json={"api_key": "k", "cheap": "m"})
    r = client.post("/api/runs", json={"ids": ids})
    assert r.status_code == 409 and "ERPNext" in r.json()["detail"]  # site not prepared yet
    assert client.get("/api/runs/nope").status_code == 404
    assert client.post("/api/runs/nope/approve", json={"task": "x"}).status_code == 409
    assert client.get("/api/requirements/stats").json()["totals"]["total"] == 2
