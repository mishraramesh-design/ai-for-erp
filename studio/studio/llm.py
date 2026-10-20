"""Provider adapters: one function per API style, JSON-only replies, token usage returned to the caller (D6, R7)."""
from __future__ import annotations

import json
import re
import time
from typing import Optional

import httpx


class LlmError(Exception):
    pass


class LlmBudgetExceeded(LlmError):
    pass


def extract_json(text: str) -> dict:
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    try:
        v = json.loads(t)
    except json.JSONDecodeError:
        a, z = t.find("{"), t.rfind("}")
        if a < 0 or z <= a:
            raise LlmError("The model did not return JSON.")
        try:
            v = json.loads(t[a:z + 1])
        except json.JSONDecodeError:
            raise LlmError("The model returned malformed JSON.")
    if not isinstance(v, dict):
        raise LlmError("The model returned JSON that is not an object.")
    return v


def _post(url: str, headers: dict, body: dict, timeout: float, retry_codes=(429, 500, 502, 503, 504)) -> httpx.Response:
    last = ""
    for attempt in range(3):
        try:
            r = httpx.post(url, json=body, headers=headers, timeout=timeout)
        except httpx.HTTPError as e:
            last = str(e)
            time.sleep(1.5 * (attempt + 1))
            continue
        if r.status_code == 200:
            return r
        last = f"HTTP {r.status_code}: {r.text[:300]}"
        if r.status_code in retry_codes:
            time.sleep(2 * (attempt + 1))
            continue
        if r.status_code == 400 and attempt == 0 and "max_completion_tokens" in body:
            body["max_tokens"] = body.pop("max_completion_tokens")  # some compatible servers want the older name
            continue
        break
    raise LlmError(f"LLM request failed: {last}")


def call_provider(kind: str, base_url: str, api_key: str, model: str, system: str, user: str,
                  max_tokens: int = 6000, timeout: float = 120.0) -> tuple[dict, dict]:
    """Returns (parsed JSON object, {"in": tokens, "out": tokens})."""
    if not api_key:
        raise LlmError("No API key is set for this provider.")
    if kind == "anthropic":
        url = (base_url or "https://api.anthropic.com").rstrip("/") + "/v1/messages"
        body = {"model": model, "max_tokens": max_tokens, "system": system,
                "messages": [{"role": "user", "content": user + "\n\nReturn ONLY one JSON object, no prose."}]}
        r = _post(url, {"x-api-key": api_key, "anthropic-version": "2023-06-01"}, body, timeout)
        d = r.json()
        text = "".join(b.get("text", "") for b in d.get("content", []) if b.get("type") == "text")
        u = d.get("usage") or {}
        return extract_json(text), {"in": int(u.get("input_tokens") or 0), "out": int(u.get("output_tokens") or 0)}
    url = (base_url or "https://api.openai.com/v1").rstrip("/") + "/chat/completions"
    body = {"model": model, "response_format": {"type": "json_object"}, "max_completion_tokens": max_tokens,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    r = _post(url, {"Authorization": f"Bearer {api_key}"}, body, timeout)
    d = r.json()
    u = d.get("usage") or {}
    return (extract_json(d["choices"][0]["message"].get("content") or ""),
            {"in": int(u.get("prompt_tokens") or 0), "out": int(u.get("completion_tokens") or 0)})
