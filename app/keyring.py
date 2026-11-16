"""Remembers which free keys are resting, and for how long.

Free tiers fail in three different ways, and they should not all be treated the
same:

* **per-minute limit** — the key is fine again in a second or two;
* **per-day limit** — the key is dead until midnight Pacific (when Google, Groq,
  OpenRouter… reset their quotas);
* **a bad key** — dead until the owner fixes it.

Without this the app would keep retrying a burned key first and waste a request
on every question. With it, a pool of free keys behaves like one bigger key: the
tutor skips the ones that are resting and keeps answering.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime, timedelta, timezone

import config as CFG

PATH = os.path.join(CFG.DATA_DIR, "key_state.json")

# Quotas reset at midnight Pacific. Windows ships no timezone database (no
# `/usr/share/zoneinfo`), so `ZoneInfo` can fail there — and a missing timezone
# must never be able to stop the app from starting. Try the real zone, then the
# `tzdata` package, then fall back to a fixed UTC-8 offset, which is close enough
# for "come back tomorrow".
try:
    from zoneinfo import ZoneInfo

    PACIFIC = ZoneInfo("America/Los_Angeles")
except Exception:  # noqa: BLE001  (missing tzdata, no system tzdb, …)
    PACIFIC = None


def _pacific_now() -> datetime:
    return datetime.now(PACIFIC) if PACIFIC is not None else datetime.now(timezone.utc) + timedelta(hours=-8)


# --------------------------------------------------------------------------- store
def _read() -> dict:
    try:
        with open(PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, dict) and v.get("until", 0) > time.time()}


def _write(data: dict) -> None:
    os.makedirs(CFG.DATA_DIR, exist_ok=True)
    tmp = PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, PATH)


def fingerprint(provider: str, key: str) -> str:
    """A stable id for a key that never reveals the key itself."""
    return hashlib.sha256(f"{provider}:{key}".encode()).hexdigest()[:16]


# --------------------------------------------------------------------------- classify
def cooldown_for(err: str) -> tuple[int, str]:
    """How long to rest a key after this error, and a short reason."""
    e = (err or "").lower()
    if any(s in e for s in ("per_day", "per day", "perday", "daily", "requests_per_day",
                            "quota exceeded", "insufficient_quota")):
        return _until_pacific_midnight(), "daily quota spent"
    if "429" in e or "rate limit" in e or "rate_limit" in e or "resource_exhausted" in e:
        return 65, "per-minute limit"
    if any(s in e for s in ("401", "403", "invalid api key", "incorrect api key",
                            "unauthorized", "authentication")):
        return 6 * 3600, "key rejected"
    return 20, "call failed"


def _until_pacific_midnight() -> int:
    now = _pacific_now()
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=5, second=0, microsecond=0)
    return max(60, int((tomorrow - now).total_seconds()))


# --------------------------------------------------------------------------- public
def mark(provider: str, key: str, err: str) -> dict:
    seconds, why = cooldown_for(err)
    data = _read()
    fp = fingerprint(provider, key)
    data[fp] = {"until": time.time() + seconds, "why": why, "provider": provider,
                "seconds": seconds}
    _write(data)
    return {"fingerprint": fp, "why": why, "seconds": seconds}


def clear(provider: str, key: str) -> None:
    data = _read()
    fp = fingerprint(provider, key)
    if fp in data:
        data.pop(fp, None)
        _write(data)


def resting(provider: str, key: str) -> dict | None:
    return _read().get(fingerprint(provider, key))


def order(chain: list[dict]) -> tuple[list[dict], list[dict]]:
    """Split a chain into (usable, resting) — usable first, original order kept."""
    state = _read()
    usable, asleep = [], []
    for cred in chain:
        fp = fingerprint(cred.get("provider", ""), cred.get("key", ""))
        (asleep if fp in state else usable).append(cred)
    return usable, asleep


def summary(chain: list[dict]) -> list[dict]:
    """Per-key state for the API/UI — never the key."""
    now = time.time()
    out = []
    for cred in chain:
        prov = cred.get("provider", "")
        st = resting(prov, cred.get("key", ""))
        out.append({
            "provider": prov,
            "state": "resting" if st else "ready",
            "why": (st or {}).get("why"),
            "until": int((st or {}).get("until", 0)),
            "minutes": max(0, int(((st or {}).get("until", now) - now) // 60)) if st else 0,
        })
    return out


def soonest_wake(chain: list[dict]) -> int:
    """Seconds until the first resting key is usable again (0 if none resting)."""
    state = _read()
    waits = [state[fingerprint(c.get("provider", ""), c.get("key", ""))]["until"] - time.time()
             for c in chain
             if fingerprint(c.get("provider", ""), c.get("key", "")) in state]
    return max(0, int(min(waits))) if waits else 0
