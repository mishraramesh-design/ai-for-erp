"""Studio HTTP service: login, model settings, the ERPNext dev site connection, and the UI."""

import hmac
import os
import threading
import time
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel

from .erp import HttpErp
from .prepare import STEPS, prepare
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
    # First start: take the ERPNext address and password from the compose file so the user does not retype them.
    if os.environ.get("ERP_ADMIN_PASSWORD") and not vault.load().get("erp", {}).get("admin_password"):
        d = vault.load()
        d["erp"] = {**d.get("erp", {}), "url": os.environ.get("ERP_URL", "http://backend:8000").rstrip("/"),
                    "site": os.environ.get("ERP_SITE", "erp.local"),
                    "admin_password": vault.enc(os.environ["ERP_ADMIN_PASSWORD"])}
        vault.save(d)
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

    # ---- the ERPNext development site
    class ErpIn(BaseModel):
        url: str = "http://backend:8000"
        site: str = "erp.local"
        admin_password: str = ""  # blank = keep stored
        company: str = "Demo Company"
        country: str = "India"
        currency: str = "INR"
        timezone: str = "Asia/Kolkata"

    progress: dict = {"steps": [], "running": False}

    def erp_public(doc: dict) -> dict:
        e = doc.get("erp", {})
        return {"url": e.get("url", "http://backend:8000"), "site": e.get("site", "erp.local"),
                "company": e.get("company", "Demo Company"), "country": e.get("country", "India"),
                "currency": e.get("currency", "INR"), "timezone": e.get("timezone", "Asia/Kolkata"),
                "password_set": bool(e.get("admin_password")), "ready": bool(e.get("api_key")),
                "running": progress["running"], "steps": progress["steps"], "step_names": STEPS}

    @app.get("/api/erp")
    def get_erp(_: str = Depends(authed)) -> dict:
        return erp_public(vault.load())

    @app.put("/api/erp")
    def put_erp(body: ErpIn, _: str = Depends(authed)) -> dict:
        doc = vault.load()
        e = doc.setdefault("erp", {})
        changed = (body.url.rstrip("/"), body.site) != (e.get("url"), e.get("site"))
        e.update(url=body.url.rstrip("/"), site=body.site, company=body.company, country=body.country,
                 currency=body.currency, timezone=body.timezone)
        if body.admin_password:
            e["admin_password"] = vault.enc(body.admin_password)
        if changed:
            e.pop("api_key", None)
            e.pop("api_secret", None)
        vault.save(doc)
        return erp_public(doc)

    def _run_prepare() -> None:
        def step(key: str, status: str, note: str) -> None:
            for s in progress["steps"]:
                if s["key"] == key:
                    s.update(status=status, note=note)
                    return
            progress["steps"].append({"key": key, "status": status, "note": note})
        try:
            doc = vault.load()
            e = doc["erp"]
            cfg = {**e, "admin_password": vault.dec(e.get("admin_password", ""))}
            keys = prepare(cfg, step)
            doc = vault.load()
            doc["erp"]["api_key"] = keys["api_key"]
            doc["erp"]["api_secret"] = vault.enc(keys["api_secret"])
            vault.save(doc)
        except Exception:  # the failing step already carries the plain-words reason
            pass
        finally:
            progress["running"] = False

    @app.post("/api/erp/prepare")
    def start_prepare(_: str = Depends(authed)) -> dict:
        e = vault.load().get("erp", {})
        if not e.get("admin_password"):
            raise HTTPException(400, "Save the Administrator password first")
        if progress["running"]:
            raise HTTPException(409, "already running")
        progress.update(running=True, steps=[])
        threading.Thread(target=_run_prepare, daemon=True).start()
        return {"ok": True}

    def erp_client() -> HttpErp:
        e = vault.load().get("erp", {})
        if not e.get("api_key"):
            raise HTTPException(409, "ERPNext is not prepared yet")
        return HttpErp(e["url"], e["api_key"], vault.dec(e["api_secret"]), host_header=e["site"])

    app.state.erp_client = erp_client

    @app.get("/api/status")
    def status(_: str = Depends(authed)) -> dict:
        doc = vault.load()
        return {"version": VERSION, "llm": llm_public(doc)["key_set"] and bool(doc["llm"].get("models")),
                "erp": bool(doc.get("erp", {}).get("api_key"))}

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
