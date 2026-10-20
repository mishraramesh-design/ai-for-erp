"""Studio HTTP service: login, model settings, the ERPNext dev site connection, and the UI."""

import hmac
import os
import threading
import time
from pathlib import Path
from typing import Optional

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel

from fastapi import File, Form, UploadFile

from .applier import Applier
from .erp import HttpErp
from .kernel import Deps, Kernel, RunStore
from .ledger import Ledger
from .models import ConfigStore, Router, UsageLog, apply_profile
from .prepare import STEPS, prepare
from .requirements import RequirementStore
from .snapshot import Snapshot
from .spec import describe
from .vault import Vault

VERSION = "0.2.0"
UI_DIR = Path(__file__).resolve().parent.parent / "ui"


def create_app(data_dir: str | None = None) -> FastAPI:
    data = data_dir or os.environ.get("STUDIO_DATA_DIR", "/data")
    vault = Vault(data, os.environ.get("STUDIO_SECRET_KEY", ""))
    admin_pw = os.environ.get("STUDIO_ADMIN_PASSWORD", "")
    app = FastAPI(title="ERPNext Studio", version=VERSION)
    app.state.vault = vault
    cfg_store = ConfigStore(Path(data) / "models.json", dict(os.environ), codec=(vault.enc, vault.dec))
    usage = UsageLog(Path(data) / "usage.jsonl")
    router = Router(cfg_store, usage)
    reqs = RequirementStore(Path(data) / "requirements.json")
    ledger = Ledger(Path(data) / "ledger")
    app.state.router, app.state.reqs, app.state.ledger = router, reqs, ledger
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

    # ---- models: one provider, a cheap and a strong model, a preset that maps them onto the agent roles
    class ModelsIn(BaseModel):
        base_url: str = "https://api.openai.com/v1"
        api_key: str = ""  # blank = keep the stored key
        cheap: str = ""
        strong: str = ""
        profile: str = "balanced"
        budget_usd: float = 0
        budget_calls: int = 5000

    def models_public() -> dict:
        c = cfg_store.get()
        prov = c.provider("main")
        dev = c.roles.get("developer")
        return {"base_url": prov.base_url if prov else "https://api.openai.com/v1", "key_set": bool(prov and prov.api_key),
                "cheap": (dev.primary.model if dev and dev.primary else ""),
                "strong": (dev.fallbacks[0].model if dev and dev.fallbacks else ""),
                "profile": app.state.profile if hasattr(app.state, "profile") else "balanced",
                "budget_usd": c.budget_usd or 0, "budget_calls": c.budget_calls,
                "roles": {r: {"enabled": rc.enabled, "primary": rc.primary.model if rc.primary else "",
                              "fallback": rc.fallbacks[0].model if rc.fallbacks else ""} for r, rc in c.roles.items()},
                "usage": usage.summary()["total"]}

    @app.get("/api/models")
    def get_models(_: str = Depends(authed)) -> dict:
        return models_public()

    @app.put("/api/models")
    def put_models(body: ModelsIn, _: str = Depends(authed)) -> dict:
        from .models import ModelConfig, Provider
        c = cfg_store.get().model_copy(deep=True)
        prov = c.provider("main")
        if not prov:
            prov = Provider(id="main", name="Model provider", kind="anthropic" if "anthropic" in body.base_url else "openai")
            c.providers = [prov]
        prov.base_url = body.base_url.rstrip("/")
        prov.kind = "anthropic" if "anthropic.com" in prov.base_url else "openai"
        if body.api_key:
            prov.api_key = body.api_key
        if body.cheap:
            apply_profile(c, body.profile if body.profile in ("economy", "balanced", "quality") else "balanced",
                          "main", body.cheap, body.strong or body.cheap)
        c.budget_usd = body.budget_usd or None
        c.budget_calls = body.budget_calls
        app.state.profile = body.profile
        cfg_store.update(c.model_dump(mode="json"))
        return models_public()

    @app.post("/api/models/test")
    def test_models(_: str = Depends(authed)) -> dict:
        m = models_public()
        if not m["key_set"] or not m["cheap"]:
            return {"ok": False, "error": "Save an API key and a model name first."}
        r = router.test("main", m["cheap"])
        if r["ok"] and m["strong"] and m["strong"] != m["cheap"]:
            r2 = router.test("main", m["strong"])
            if not r2["ok"]:
                return {"ok": False, "error": f"The stronger model failed: {r2['error']}"}
        return r

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
        return {"version": VERSION, "llm": router.enabled("developer"), "requirements": len(reqs.all()),
                "erp": bool(doc.get("erp", {}).get("api_key"))}

    @app.get("/api/overview")
    def overview(_: str = Depends(authed)) -> dict:
        st = reqs.stats()
        sets = ledger.list(limit=5000)
        applied = [c for c in sets if c.status == "applied"]
        verified = [r for r in reqs.all() if r.status == "verified"]
        runs = kernel.store.list_ids()
        return {"totals": st["totals"], "modules": st["modules"][:12],
                "change_sets": {"total": len(sets), "applied": len(applied),
                                "undone": sum(1 for c in sets if c.status == "undone")},
                "usage": usage.summary()["total"], "by_role": usage.summary()["by_role"],
                "verified": [{"id": r.id, "text": r.text[:160], "module": r.module, "evidence": len(r.evidence)} for r in verified[-8:]],
                "decisions": {"auto": sum(1 for c in applied if c.risk() != "high"), "human": sum(1 for c in applied if c.risk() == "high")},
                "runs": len(runs)}

    # ---- requirements
    @app.get("/api/requirements/stats")
    def req_stats(_: str = Depends(authed)) -> dict:
        return reqs.stats()

    @app.get("/api/requirements")
    def req_list(module: str = "", status: str = "", q: str = "", gap: bool = True, limit: int = 40, offset: int = 0,
                 ids: str = "", _: str = Depends(authed)) -> dict:
        if ids:
            want = [int(x) for x in ids.split(",") if x.strip().isdigit()][:200]
            items = [reqs.get(i) for i in want]
            return {"total": len(items), "items": [r.model_dump(mode="json") for r in items if r]}
        return reqs.query(module=module, gap_only=gap, status=status, q=q, limit=min(limit, 200), offset=offset)

    @app.post("/api/requirements/import")
    async def req_import(file: UploadFile = File(...), module: str = Form(""), _: str = Depends(authed)) -> dict:
        data = await file.read()
        name = (file.filename or "").lower()
        if len(data) > 25_000_000:
            raise HTTPException(413, "file too large")
        try:
            if name.endswith((".xlsx", ".xlsm")):
                return reqs.import_matrix(data)
            if not module.strip():
                raise HTTPException(400, "Give the module name these requirements belong to (for example: SRM).")
            if name.endswith(".docx"):
                import io
                import docx
                text = "\n".join(p.text for p in docx.Document(io.BytesIO(data)).paragraphs)
            else:
                text = data.decode("utf-8", "ignore")
            return reqs.import_text(text, module.strip(), source=file.filename or "upload")
        except HTTPException:
            raise
        except Exception as e:  # unreadable file: say so in words
            raise HTTPException(400, f"Could not read that file: {str(e)[:150]}")

    class TextIn(BaseModel):
        text: str
        module: str

    @app.post("/api/requirements/text")
    def req_text(body: TextIn, _: str = Depends(authed)) -> dict:
        if not body.module.strip() or not body.text.strip():
            raise HTTPException(400, "Module and text are required.")
        return reqs.import_text(body.text, body.module.strip(), source="pasted")

    # ---- runs (the kernel)
    erp_cache: dict = {}

    def build_deps() -> Deps:
        e = vault.load().get("erp", {})
        if not e.get("api_key"):
            raise ValueError("Prepare the ERPNext site first (ERPNext screen).")
        key = (e["url"], e["site"], e["api_key"])
        if erp_cache.get("key") != key:
            erp = HttpErp(e["url"], e["api_key"], vault.dec(e["api_secret"]), host_header=e["site"])
            snap = Snapshot(erp)
            erp_cache.update(key=key, erp=erp, snap=snap, applier=Applier(erp, snap))
        return Deps(reqs=reqs, ledger=ledger, erp=erp_cache["erp"], snap=erp_cache["snap"], applier=erp_cache["applier"],
                    llm_for=lambda role, run_id: router.for_role(role, project="default", run_id=run_id),
                    llm_calls=lambda rid: usage.summary(run_id=rid)["total"]["calls"],
                    roles_enabled=router.enabled)

    kernel = Kernel(RunStore(Path(data) / "runs"), build_deps)
    app.state.kernel = kernel

    class RunIn(BaseModel):
        ids: Optional[list[int]] = None
        module: str = ""
        branch: str = ""
        process: str = ""
        limit: int = 12
        policy: dict = {}
        guidance: str = ""

    @app.post("/api/runs")
    def start_run(body: RunIn, _: str = Depends(authed)) -> dict:
        if not router.enabled("developer"):
            raise HTTPException(409, "Connect a model first (Models screen).")
        if body.ids is not None and not body.ids:
            raise HTTPException(400, "No requirements selected.")
        ids = body.ids or [r.id for r in reqs.batch(module=body.module, branch=body.branch, process=body.process,
                                                    limit=min(body.limit, 60))]
        try:
            run = kernel.start("default", ids, body.policy, body.guidance)
        except ValueError as e:
            raise HTTPException(409, str(e))
        return kernel.public(run)

    @app.get("/api/runs")
    def list_runs(_: str = Depends(authed)) -> list:
        out = []
        for rid in kernel.store.list_ids()[:30]:
            r = kernel.get(rid)
            if r:
                out.append({"id": r.id, "title": r.title, "status": r.status, "created": r.created,
                            "groups": len(r.groups), "events": len(r.events)})
        return out

    @app.get("/api/runs/{rid}")
    def get_run(rid: str, since: int = 0, _: str = Depends(authed)) -> dict:
        r = kernel.get(rid)
        if not r:
            raise HTTPException(404, "unknown run")
        d = kernel.public(r, since)
        d["usage"] = usage.summary(run_id=rid)["total"]
        d["changesets"] = {gid: _cs_view(g["cs_id"]) for gid, g in r.groups.items() if g.get("cs_id")}
        return d

    def _cs_view(cs_id: str) -> dict:
        cs = ledger.get(cs_id)
        if not cs:
            return {}
        pub = cs.to_public()
        return {"id": cs.id, "title": cs.title, "status": cs.status, "risk": pub["risk"], "lines": pub["lines"],
                "assumptions": cs.assumptions, "not_covered": cs.not_covered, "warnings": cs.warnings, "error": cs.error}

    class DecisionIn(BaseModel):
        task: str
        reason: str = ""

    @app.post("/api/runs/{rid}/approve")
    def approve(rid: str, body: DecisionIn, user: str = Depends(authed)) -> dict:
        try:
            return kernel.public(kernel.approve(rid, body.task, user))
        except (KeyError, ValueError) as e:
            raise HTTPException(409, str(e))

    @app.post("/api/runs/{rid}/reject")
    def reject(rid: str, body: DecisionIn, user: str = Depends(authed)) -> dict:
        try:
            return kernel.public(kernel.reject(rid, body.task, user, body.reason))
        except (KeyError, ValueError) as e:
            raise HTTPException(409, str(e))

    @app.post("/api/runs/{rid}/cancel")
    def cancel(rid: str, _: str = Depends(authed)) -> dict:
        try:
            return kernel.public(kernel.cancel(rid))
        except KeyError:
            raise HTTPException(404, "unknown run")

    @app.post("/api/changesets/{cid}/undo")
    def undo(cid: str, user: str = Depends(authed)) -> dict:
        try:
            return kernel.undo(cid, user)
        except ValueError as e:
            raise HTTPException(409, str(e))

    # ---- export: verified change sets -> csd_customizations -> pull request
    import io
    import zipfile

    from . import exporter
    from .github import GitHub, GitHubError

    def gh_cfg() -> dict:
        d = vault.load().get("github") or {}
        return {**d, "token": vault.dec(d["token"]) if d.get("token") else ""}

    def gh_client():
        c = gh_cfg()
        if not (c.get("repo") and c.get("token")):
            raise HTTPException(409, "Add the GitHub repository and token first (Export screen).")
        try:
            return GitHub(c["repo"], c["token"], c.get("base") or "main")
        except GitHubError as e:
            raise HTTPException(400, str(e))

    @app.get("/api/github")
    def github_get(_: str = Depends(authed)) -> dict:
        c = gh_cfg()
        return {"repo": c.get("repo", ""), "base": c.get("base", "main"), "token_set": bool(c.get("token"))}

    class GhIn(BaseModel):
        repo: str = ""
        base: str = "main"
        token: str = ""

    @app.put("/api/github")
    def github_put(body: GhIn, _: str = Depends(authed)) -> dict:
        d = vault.load()
        cur = d.get("github") or {}
        d["github"] = {"repo": body.repo.strip(), "base": body.base.strip() or "main",
                       "token": vault.enc(body.token.strip()) if body.token.strip() else cur.get("token", "")}
        vault.save(d)
        return github_get()

    @app.post("/api/github/test")
    def github_test(_: str = Depends(authed)) -> dict:
        try:
            return gh_client().check()
        except GitHubError as e:
            raise HTTPException(400, str(e))

    def current_export(only_verified: bool = True):
        hooks = ""
        c = gh_cfg()
        if c.get("repo") and c.get("token"):
            try:
                hooks = gh_client().get_file(exporter.PKG_PREFIX + "hooks.py") or ""
            except (GitHubError, httpx.HTTPError):
                hooks = ""
        return exporter.build(ledger, reqs, hooks, only_verified=only_verified)

    @app.get("/api/export")
    def export_preview(only_verified: bool = True, _: str = Depends(authed)) -> dict:
        e = current_export(only_verified)
        sets = {c.id: c for c in ledger.list(limit=5000)}
        return {**e.summary(), "change_sets": e.change_sets, "items": [{"id": i, "title": sets[i].title, "risk": sets[i].risk(),
                                          "requirements": sets[i].requirement_ids, "lines": [describe(o) for o in sets[i].ops]}
                                         for i in e.change_sets if i in sets]}

    @app.get("/api/export/bundle")
    def export_bundle(only_verified: bool = True, _: str = Depends(authed)) -> Response:
        e = current_export(only_verified)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for path, text in sorted(e.files.items()):
                z.writestr(path, text)
        return Response(buf.getvalue(), media_type="application/zip",
                        headers={"Content-Disposition": 'attachment; filename="csd_customizations_export.zip"'})

    @app.get("/api/export/drift")
    def export_drift(only_verified: bool = True, _: str = Depends(authed)) -> dict:
        e = current_export(only_verified)
        rows = exporter.drift(e.docs, build_deps().erp)
        return {"items": rows, "differs": sum(1 for r in rows if r["state"] in ("missing", "changed"))}

    class PublishIn(BaseModel):
        only_verified: bool = True

    @app.post("/api/export/publish")
    def export_publish(body: PublishIn, user: str = Depends(authed)) -> dict:
        e = current_export(body.only_verified)
        if not e.change_sets:
            raise HTTPException(409, "Nothing verified to export yet.")
        n = len(e.change_sets)
        text = "\n".join(f"- {k}: {v}" for k, v in e.counts.items())
        try:
            res = gh_client().publish(e.files, exporter.OWNED, f"Studio export: {n} verified change set(s)",
                                      f"Generated by the ERPNext Studio for {user}.\n\n{text}\n\nChange sets: " + ", ".join(e.change_sets)
                                      + ("\n\n**Before release:** this export contains Server Scripts. Frappe v15 only runs them when `server_script_enabled` is true in "
                                         "`sites/common_site_config.json` on the target bench; otherwise saving the affected documents fails." if "Server Script" in e.counts else "")
                                      + "\n\nReview, merge, and let CI build the release image. Production is never written by the Studio.")
        except GitHubError as ex:
            raise HTTPException(502, str(ex))
        return res

    @app.get("/api/export/pr/{number}")
    def export_pr(number: int, _: str = Depends(authed)) -> dict:
        try:
            return gh_client().pr_state(number)
        except GitHubError as ex:
            raise HTTPException(502, str(ex))

    @app.post("/api/export/pr/{number}/merge")
    def export_merge(number: int, _: str = Depends(authed)) -> dict:
        try:
            return gh_client().merge(number)
        except GitHubError as ex:
            raise HTTPException(502, str(ex))

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
