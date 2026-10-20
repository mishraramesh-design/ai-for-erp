"""Encrypted settings store. Secrets are encrypted with a key derived from STUDIO_SECRET_KEY
(or a key generated on first start and kept in the data volume)."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

SECRET_FIELDS = {"llm_api_key", "ssh_password", "ssh_private_key", "erp_api_secret"}


class Vault:
    def __init__(self, data_dir: str | Path, secret: str = ""):
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        if not secret:
            kf = self.dir / ".secret_key"
            if not kf.exists():
                kf.write_text(secrets.token_urlsafe(48))
                kf.chmod(0o600)
            secret = kf.read_text().strip()
        self.secret = secret
        self._f = Fernet(base64.urlsafe_b64encode(hashlib.sha256(b"enc:" + secret.encode()).digest()))
        self.path = self.dir / "settings.json"

    # --- encryption of single values
    def enc(self, v: str) -> str:
        return self._f.encrypt(v.encode()).decode()

    def dec(self, v: str) -> str:
        try:
            return self._f.decrypt(v.encode()).decode()
        except InvalidToken:
            return ""

    # --- settings document {"llm": {...}, "servers": [...]}
    def load(self) -> dict:
        if not self.path.exists():
            return {"llm": {}, "servers": []}
        return json.loads(self.path.read_text())

    def save(self, doc: dict) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc, indent=1))
        tmp.chmod(0o600)
        tmp.replace(self.path)

    # --- session cookies (stateless, signed)
    def sign(self, user: str, ttl: int = 12 * 3600) -> str:
        exp = str(int(time.time()) + ttl)
        body = f"{user}|{exp}"
        sig = hmac.new(self.secret.encode(), body.encode(), hashlib.sha256).hexdigest()
        return base64.urlsafe_b64encode(f"{body}|{sig}".encode()).decode()

    def verify(self, token: str) -> str:
        try:
            user, exp, sig = base64.urlsafe_b64decode(token.encode()).decode().split("|")
        except Exception:
            return ""
        good = hmac.new(self.secret.encode(), f"{user}|{exp}".encode(), hashlib.sha256).hexdigest()
        return user if hmac.compare_digest(sig, good) and int(exp) > time.time() else ""
