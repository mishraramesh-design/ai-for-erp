"""Studio HTTP service: login, settings (model access), servers, and the UI."""

import hmac
import os
import secrets
import time
import uuid
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel

from .vault import Vault

VERSION = "0.2.0"
UI_DIR = Path(__file__).resolve().parent.parent / "ui"
ROLES = ["architect", "builder", "customize", "uiux", "reviewer", "tester", "docs"]


def create_app(data_dir: str | None = None) -> FastAPI:
    data = data_dir or os.environ.get("STUDIO_DATA_DIR", "/data")
    vault = Vault(data, os.environ.get("STUDIO_SECRET_KEY", ""))
    admin_pw = os.environ.get("STUDIO_ADMIN_PASSWORD", "")
    app = FastAPI(title="ERPNext Studio", version=VERSION)
    app.state.vault = vault
    fails: dict[str, list[float]] = {}

    def page(name: str) -> HTMLResponse:
        return HTMLResponse((UI_DIR / name).read_text(encoding="utf-8"))

    def authed(request: Request) -> str:
        u = vault.verify(request.cookies.get("studio_session", ""))
        if not u:
            raise HTTPException(401, "login required")
        return u

    @app.get("/health")
    def health() -> dict:
        return {"ok": True, "version": VERSION, "login_configured": bool(admin_pw)}

    class Login(BaseModel):
        password: str

    @app.post("/api/login")
    def login(body: Login, request: Request, response: Response) -> dict:
        ip = request.client.host if request.client else "?"
        now = time.time()
        recent = [t for t in fails.get(ip, []) if now - t < 300]
        if len(recent) >= 5:
            raise HTTPException(429, "too many attempts, wait a few minutes")
        if not admin_pw or not hmac.compare_digest(body.password, admin_pw):
            fails[ip] = recent + [now]
            raise HTTPException(401, "wrong password")
        response.set_cookie("studio_session", vault.sign("admin"), httponly=True, samesite="strict", max_age=12 * 3600)
        return {"ok": True}

    @app.post("/api/logout")
    def logout(response: Response) -> dict:
        response.delete_cookie("studio_session")
        return {"ok": True}

    # ---- settings: model access
    class LlmIn(BaseModel):
        base_url: str = "https://api.openai.com/v1"
        api_key: str = ""  # blank = keep the stored key
        models: dict[str, str] = {}
        monthly_budget_usd: float = 0

    def llm_public(doc: dict) -> dict:
        l = doc.get("llm", {})
        return {"base_url": l.get("base_url", "https://api.openai.com/v1"), "models": l.get("models", {}),
                "monthly_budget_usd": l.get("monthly_budget_usd", 0), "key_set": bool(l.get("llm_api_key")),
                "roles": ROLES}

    @app.get("/api/settings/llm")
    def get_llm(_: str = Depends(authed)) -> dict:
        return llm_public(vault.load())

    @app.put("/api/settings/llm")
    def put_llm(body: LlmIn, _: str = Depends(authed)) -> dict:
        doc = vault.load()
        l = doc.setdefault("llm", {})
        l.update(base_url=body.base_url.rstrip("/"), models={k: v for k, v in body.models.items() if v},
                 monthly_budget_usd=body.monthly_budget_usd)
        if body.api_key:
            l["llm_api_key"] = vault.enc(body.api_key)
        vault.save(doc)
        return llm_public(doc)

    @app.post("/api/settings/llm/test")
    def test_llm(_: str = Depends(authed)) -> dict:
        l = vault.load().get("llm", {})
        key = vault.dec(l.get("llm_api_key", ""))
        model = (l.get("models") or {}).get("architect") or next(iter((l.get("models") or {}).values()), "")
        if not key or not model:
            return {"ok": False, "error": "Save an API key and at least one model first."}
        try:
            r = httpx.post(l.get("base_url", "") + "/chat/completions", timeout=30,
                           headers={"Authorization": f"Bearer {key}"},
                           json={"model": model, "messages": [{"role": "user", "content": "Reply with: ok"}],
                                 "max_tokens": 5})
            if r.status_code != 200:
                return {"ok": False, "error": f"HTTP {r.status_code}: {r.text[:200]}"}
            return {"ok": True, "model": model, "reply": r.json()["choices"][0]["message"]["content"]}
        except Exception as e:  # network or parse errors are reported, never raised
            return {"ok": False, "error": str(e)[:200]}

    # ---- servers (ERPNext targets)
    class ServerIn(BaseModel):
        name: str
        host: str
        ssh_port: int = 22
        ssh_user: str = "root"
        ssh_password: str = ""
        ssh_private_key: str = ""
        role: str = "development"

    def server_public(s: dict) -> dict:
        return {k: v for k, v in s.items() if k not in ("ssh_password", "ssh_private_key")} | {
            "has_password": bool(s.get("ssh_password")), "has_key": bool(s.get("ssh_private_key"))}

    @app.get("/api/servers")
    def list_servers(_: str = Depends(authed)) -> list:
        return [server_public(s) for s in vault.load()["servers"]]

    @app.post("/api/servers")
    def add_server(body: ServerIn, _: str = Depends(authed)) -> dict:
        doc = vault.load()
        s = body.model_dump()
        s["id"] = uuid.uuid4().hex[:8]
        s["status"] = "not installed"
        for f in ("ssh_password", "ssh_private_key"):
            s[f] = vault.enc(s[f]) if s[f] else ""
        doc["servers"].append(s)
        vault.save(doc)
        return server_public(s)

    @app.delete("/api/servers/{sid}")
    def del_server(sid: str, _: str = Depends(authed)) -> dict:
        doc = vault.load()
        doc["servers"] = [s for s in doc["servers"] if s["id"] != sid]
        vault.save(doc)
        return {"ok": True}

    @app.get("/api/status")
    def status(_: str = Depends(authed)) -> dict:
        doc = vault.load()
        return {"version": VERSION, "llm": llm_public(doc)["key_set"] and bool(doc["llm"].get("models")),
                "servers": len(doc["servers"])}

    # ---- pages
    def gated(name: str):
        def view(request: Request):
            if not vault.verify(request.cookies.get("studio_session", "")):
                return RedirectResponse("/login", status_code=303)
            return page(name)
        return view

    app.get("/login", response_class=HTMLResponse)(lambda: page("login.html"))
    app.get("/", response_class=HTMLResponse)(gated("home.html"))
    app.get("/workbench", response_class=HTMLResponse)(gated("workbench.html"))
    return app


app = create_app() if os.environ.get("STUDIO_DATA_DIR") or Path("/data").exists() else create_app("./data")
