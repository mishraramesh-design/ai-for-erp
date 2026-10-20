"""Studio HTTP service. Endpoints are added per component; this module wires them together."""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

app = FastAPI(title="ERPNext Studio", version="0.1.0")
UI_DIR = Path(__file__).resolve().parent.parent / "ui"


@app.get("/health")
def health() -> dict:
    return {"ok": True, "version": app.version, "data_dir": os.environ.get("STUDIO_DATA_DIR", "/data")}


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    page = UI_DIR / "index.html"
    return page.read_text(encoding="utf-8") if page.exists() else "<h1>ERPNext Studio</h1><p>UI not built yet.</p>"
