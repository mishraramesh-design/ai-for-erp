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


def test_llm_key_encrypted_and_never_returned(client, tmp_path):
    login(client)
    r = client.put("/api/settings/llm", json={"api_key": "sk-secret-123", "models": {"architect": "m1"}}).json()
    assert r["key_set"] and "sk-secret-123" not in str(r)
    assert "sk-secret-123" not in (tmp_path / "settings.json").read_text()
    client.put("/api/settings/llm", json={"api_key": "", "models": {"architect": "m2"}})
    assert client.get("/api/settings/llm").json()["key_set"]  # blank keeps stored key
    assert client.get("/api/status").json()["llm"] is True


def test_servers_credentials_hidden(client, tmp_path):
    login(client)
    s = client.post("/api/servers", json={"name": "d", "host": "1.2.3.4", "ssh_password": "hunter2"}).json()
    assert s["has_password"] and "ssh_password" not in s
    assert "hunter2" not in (tmp_path / "settings.json").read_text()
    assert len(client.get("/api/servers").json()) == 1
    client.delete(f"/api/servers/{s['id']}")
    assert client.get("/api/servers").json() == []


def test_pages_after_login(client):
    login(client)
    for p in ("/", "/workbench"):
        assert client.get(p).status_code == 200
