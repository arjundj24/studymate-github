"""A single, owner-provided AI key that every visitor can use.

Two ways to supply it:

* environment (`STUDYMATE_LLM_KEY` …) — best for Hugging Face Spaces, since the
  secret never lands on the container's disk;
* the app itself (Settings → Site key) — writes `data/site_key.json`, protected
  by an owner passcode, so the key survives restarts through the backup snapshot.

Visitor keys still win: if someone pastes their own key in the browser, that key
is used for their requests and the site key is untouched.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import time
from collections import defaultdict
from datetime import date

import config as CFG
import llm

PATH = os.path.join(CFG.DATA_DIR, "site_key.json")
_usage: dict[str, list] = defaultdict(lambda: [str(date.today()), 0])


# --------------------------------------------------------------------------- store
def _read() -> dict:
    try:
        with open(PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _write(data: dict) -> None:
    os.makedirs(CFG.DATA_DIR, exist_ok=True)
    tmp = PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.chmod(tmp, 0o600)
    os.replace(tmp, PATH)


def _hash(passcode: str, salt: str) -> str:
    return hashlib.sha256((salt + passcode).encode()).hexdigest()


# --------------------------------------------------------------------------- public view
def _backup_view(items: list[dict]) -> list[dict]:
    return [{"provider": c.get("provider"), 
             "label": llm.PROVIDERS.get(c.get("provider"), {}).get("label", c.get("provider")),
             "model": c.get("model") or llm.DEFAULT_MODELS.get(c.get("provider", ""), "")}
            for c in items]


def status() -> dict:
    """What the front end is allowed to know — never the key itself."""
    if CFG.SITE_KEY:
        prov = CFG.SITE_PROVIDER
        return {"site": True, "source": "server", "managed": True, "provider": prov,
                "model": CFG.SITE_MODEL or llm.DEFAULT_MODELS.get(prov, ""),
                "free": llm.PROVIDERS.get(prov, {}).get("free", False),
                "backups": _backup_view(CFG.BACKUP_KEYS),
                "limit": CFG.DAILY_LIMIT}
    data = _read()
    if data.get("key"):
        prov = data.get("provider") or "gemini"
        return {"site": True, "source": "app", "managed": False, "provider": prov,
                "model": data.get("model") or llm.DEFAULT_MODELS.get(prov, ""),
                "free": llm.PROVIDERS.get(prov, {}).get("free", False),
                "backups": _backup_view(data.get("backups") or []),
                "limit": CFG.DAILY_LIMIT}
    return {"site": False, "source": None, "managed": False, "provider": None, "model": None,
            "free": True, "backups": [], "limit": CFG.DAILY_LIMIT}


def credentials() -> dict | None:
    """The key to use server-side, or None."""
    if CFG.SITE_KEY:
        return {"provider": CFG.SITE_PROVIDER, "key": CFG.SITE_KEY,
                "model": CFG.SITE_MODEL or None, "base_url": CFG.SITE_BASE or None}
    data = _read()
    if data.get("key"):
        return {"provider": data.get("provider") or "gemini", "key": data["key"],
                "model": data.get("model") or None, "base_url": data.get("base_url") or None}
    return None


def credentials_chain() -> list[dict]:
    """Every server-side key in priority order: primary first, then the backups.

    The tutor walks this list and falls over to the next free key when one hits a
    quota (429), an expired token or a network error — so a free deployment keeps
    answering after Gemini's 1,500-a-day runs out and Groq takes over.
    """
    chain: list[dict] = []
    if CFG.SITE_KEY:
        chain.append({"provider": CFG.SITE_PROVIDER, "key": CFG.SITE_KEY,
                      "model": CFG.SITE_MODEL or None, "base_url": CFG.SITE_BASE or None})
        chain.extend(dict(c) for c in CFG.BACKUP_KEYS)
        return chain
    data = _read()
    if data.get("key"):
        chain.append({"provider": data.get("provider") or "gemini", "key": data["key"],
                      "model": data.get("model") or None, "base_url": data.get("base_url") or None})
        chain.extend(dict(c) for c in (data.get("backups") or []))
    return chain


# --------------------------------------------------------------------------- owner actions
def save(api_key: str, provider: str, model: str | None, base_url: str | None,
         passcode: str | None, new_passcode: str | None = None,
         backups: list[dict] | None = None) -> dict:
    if CFG.SITE_KEY:
        raise ValueError("This site's key is managed by the server (STUDYMATE_LLM_KEY) and cannot be changed here.")
    api_key = (api_key or "").strip()
    is_local = (provider or "").strip().lower() == "local"
    if is_local and not api_key:
        api_key = "local"                       # Ollama needs no key at all
    if not is_local and len(api_key) < 6:
        raise ValueError("That does not look like an API key (too short).")
    data = _read()
    if data.get("key"):                                  # already configured → must prove ownership
        if not passcode or _hash(passcode, data.get("salt", "")) != data.get("passcode"):
            raise PermissionError("Wrong owner passcode.")
    else:
        passcode = (passcode or new_passcode or "").strip()
        if len(passcode) < 4:
            raise ValueError("Choose an owner passcode (at least 4 characters).")
        data = {"salt": secrets.token_hex(8)}
        data["passcode"] = _hash(passcode, data["salt"])
    if new_passcode:
        data["salt"] = secrets.token_hex(8)
        data["passcode"] = _hash(new_passcode.strip(), data["salt"])
    provider = (provider or "gemini").strip().lower()
    if provider not in llm.PROVIDERS:
        raise ValueError("Unknown provider.")
    if provider == "custom" and not (base_url or "").strip():
        raise ValueError("A self-hosted/custom provider needs a base URL.")
    clean_backups: list[dict] = []
    for b in (backups or [])[:6]:
        if not isinstance(b, dict):
            continue
        bkey = (b.get("api_key") or b.get("key") or "").strip()
        bprov = (b.get("provider") or "").strip().lower()
        if not bkey or bprov not in llm.PROVIDERS:
            continue
        if len(bkey) < 6:
            raise ValueError("One of the backup keys is too short to be real.")
        if bprov == "custom" and not (b.get("base_url") or "").strip():
            raise ValueError("A custom backup key needs a base URL.")
        clean_backups.append({"provider": bprov, "key": bkey,
                              "model": (b.get("model") or "").strip() or None,
                              "base_url": (b.get("base_url") or "").strip() or None})
    data.update({"key": api_key, "provider": provider, "model": (model or "").strip() or None,
                 "base_url": (base_url or "").strip() or None, "created": time.time(),
                 "backups": clean_backups})
    _write(data)
    return status()


def clear(passcode: str | None) -> dict:
    if CFG.SITE_KEY:
        raise ValueError("This site's key is managed by the server (STUDYMATE_LLM_KEY).")
    data = _read()
    if not data.get("key"):
        return status()
    if not passcode or _hash(passcode, data.get("salt", "")) != data.get("passcode"):
        raise PermissionError("Wrong owner passcode.")
    _write({})
    return status()


def check_passcode(passcode: str | None) -> bool:
    data = _read()
    if not data.get("key"):
        return True
    return bool(passcode) and _hash(passcode, data.get("salt", "")) == data.get("passcode")


# --------------------------------------------------------------------------- rate limit
def within_limit(ip: str) -> bool:
    """Per-IP daily ceiling so a shared free key cannot be drained."""
    today = str(date.today())
    entry = _usage[ip]
    if entry[0] != today:
        entry[0], entry[1] = today, 0
    if entry[1] >= CFG.DAILY_LIMIT:
        return False
    entry[1] += 1
    return True


def usage_left(ip: str) -> int:
    entry = _usage[ip]
    if entry[0] != str(date.today()):
        return CFG.DAILY_LIMIT
    return max(0, CFG.DAILY_LIMIT - entry[1])
