"""Optional free online AI for writing hooks: Groq (groq.com — not "Grok" from xAI).

Groq's free tier needs no card (about 1,000 requests a day on its bigger open models). The key lives in the macOS
Keychain / Windows Credential Manager and is never sent to the page. Using it sends the clip's description and transcript to Groq.
Any problem (no internet, daily limit, bad key) raises OnlineAIError, and the caller falls back to the local AI.
"""
from __future__ import annotations

import json
import os
import re

import requests

BASE_URL = os.environ.get("REELMAKER_GROQ_URL", "https://api.groq.com/openai/v1")
KEYCHAIN_SERVICE = os.environ.get("REELMAKER_GROQ_KEYCHAIN", "Reelmaker Groq API key")
KEYCHAIN_ACCOUNT = "reelmaker"
# best first; the first one your account actually has is used unless you pick another
PREFERRED_MODELS = ["openai/gpt-oss-120b", "openai/gpt-oss-20b", "qwen/qwen3-32b", "llama-3.3-70b-versatile"]


class OnlineAIError(RuntimeError):
    pass


# ------------------------------------------------------------------ the key (Keychain)

def get_key() -> tuple[str | None, str | None]:
    """(key, "keychain" | "env" | None). "keychain" = the macOS Keychain or Windows Credential Manager."""
    from . import system
    k = system.secret_get(KEYCHAIN_SERVICE)
    if k:
        return k, "keychain"
    k = os.environ.get("GROQ_API_KEY", "").strip()
    return (k, "env") if k else (None, None)


def save_key(key: str) -> None:
    """Store the key in the macOS Keychain / Windows Credential Manager."""
    from . import system
    key = key.strip()
    if not key or not re.fullmatch(r"[A-Za-z0-9_\-]{20,200}", key):
        raise OnlineAIError("That doesn't look like a Groq API key (they start with gsk_).")
    try:
        system.secret_set(KEYCHAIN_SERVICE, key)
    except Exception as e:
        raise OnlineAIError(f"Couldn't save the key in {system.SECRET_STORE}: {str(e)[:160]}")
    if get_key()[1] != "keychain":
        raise OnlineAIError(f"Couldn't save the key in {system.SECRET_STORE}.")


def delete_key() -> None:
    from . import system
    system.secret_delete(KEYCHAIN_SERVICE)


# ------------------------------------------------------------------ API

def _err(r: requests.Response) -> OnlineAIError:
    try:
        msg = (r.json().get("error") or {}).get("message") or r.text
    except Exception:
        msg = r.text
    msg = str(msg)[:200]
    if r.status_code == 401:
        return OnlineAIError("Groq says this API key is not valid.")
    if r.status_code == 429:
        return OnlineAIError("Groq's free limit is reached for now (per minute or per day) — using the local AI meanwhile.")
    if r.status_code == 404:
        return OnlineAIError(f"Groq doesn't offer that model to your account: {msg}")
    return OnlineAIError(f"Groq error ({r.status_code}): {msg}")


def list_models(key: str) -> list[str]:
    """Models your Groq account can use (checking costs nothing — also how a new key is verified)."""
    try:
        r = requests.get(f"{BASE_URL}/models", headers={"Authorization": f"Bearer {key}"}, timeout=20)
    except requests.RequestException:
        raise OnlineAIError("Couldn't reach Groq — check your internet connection.")
    if r.status_code != 200:
        raise _err(r)
    ids = [m.get("id") for m in r.json().get("data", []) if m.get("id") and m.get("active", True)]
    # text models only (skip speech, guard and vision-only models)
    ids = [i for i in ids if not re.search(r"whisper|tts|guard|playai|orpheus|distil", i, re.I)]
    return sorted(ids, key=lambda i: (PREFERRED_MODELS.index(i) if i in PREFERRED_MODELS else 99, i))


def pick_model(available: list[str], wanted: str | None) -> str | None:
    if wanted and wanted in available:
        return wanted
    return next((m for m in PREFERRED_MODELS if m in available), available[0] if available else None)


def chat_json(prompt: str, model: str, temperature: float = 0.7, timeout: float = 90) -> dict:
    """One prompt in, one JSON object out."""
    key, _ = get_key()
    if not key:
        raise OnlineAIError("No Groq API key saved.")
    body = {"model": model, "temperature": temperature, "max_completion_tokens": 4096,
            "messages": [{"role": "user", "content": prompt}], "response_format": {"type": "json_object"}}
    if "gpt-oss" in model:
        body["reasoning_effort"] = "medium"
    hdr = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    try:
        r = requests.post(f"{BASE_URL}/chat/completions", headers=hdr, json=body, timeout=timeout)
        if r.status_code == 400 and "response_format" in r.text:   # a model without JSON mode: ask plainly
            body.pop("response_format", None)
            r = requests.post(f"{BASE_URL}/chat/completions", headers=hdr, json=body, timeout=timeout)
    except requests.RequestException:
        raise OnlineAIError("Couldn't reach Groq — check your internet connection.")
    if r.status_code != 200:
        raise _err(r)
    try:
        text = r.json()["choices"][0]["message"]["content"] or ""
    except Exception:
        raise OnlineAIError("Groq's answer was empty.")
    m = re.search(r"\{.*\}", text, re.S)   # tolerate text around the JSON
    try:
        return json.loads(m.group(0) if m else text)
    except Exception:
        raise OnlineAIError("Groq's answer wasn't readable JSON.")
