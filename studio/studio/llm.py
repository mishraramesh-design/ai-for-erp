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


def reasoning_effort(model: str, base_url: str = "") -> str:
    """Hidden thinking is billed as output. Keep it low for OpenAI reasoning models (override: STUDIO_REASONING_EFFORT,
    or 'off'); medium for the full-size model that repairs use."""
    import os
    forced = os.environ.get("STUDIO_REASONING_EFFORT", "").strip().lower()
    if forced:
        return "" if forced == "off" else forced
    m = (model or "").lower()
    if "api.openai.com" not in (base_url or "https://api.openai.com") or not m.startswith(("gpt-5", "o1", "o3", "o4")):
        return ""
    return "low" if "mini" in m or "nano" in m else "medium"


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
    # Reasoning models spend completion tokens on hidden thinking; when the budget runs out the answer is empty
    # (finish_reason "length"). Retry once with a larger budget instead of failing the whole task.
    budget = max(max_tokens, 12000)
    for attempt in range(2):
        body = {"model": model, "response_format": {"type": "json_object"}, "max_completion_tokens": budget,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        effort = reasoning_effort(model, base_url)
        if effort:
            body["reasoning_effort"] = effort
        try:
            r = _post(url, {"Authorization": f"Bearer {api_key}"}, body, max(timeout, 240.0))
        except LlmError:
            if not effort:
                raise
            body.pop("reasoning_effort")  # a compatible server that does not know the parameter
            r = _post(url, {"Authorization": f"Bearer {api_key}"}, body, max(timeout, 240.0))
        d = r.json()
        u = d.get("usage") or {}
        choice = d["choices"][0]
        text = choice["message"].get("content") or ""
        if text.strip() or choice.get("finish_reason") != "length" or attempt:
            break
        budget *= 2
    if not text.strip():
        raise LlmError(f"The model returned no answer (finish reason: {choice.get('finish_reason')}; token budget {budget}).")
    return (extract_json(text), {"in": int(u.get("prompt_tokens") or 0), "out": int(u.get("completion_tokens") or 0)})
