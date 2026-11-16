"""Environment-driven settings for StudyMate.

Everything a host needs to configure lives here, so the code never hard-codes a
port, a path or an origin again. All values are optional — the defaults are what
you get when you run it locally.
"""
from __future__ import annotations

import os
from pathlib import Path


def _bool(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    try:
        return int(str(os.environ.get(name, default)).strip())
    except (TypeError, ValueError):
        return default


HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

# Where documents, page renders and the workspace live. Point this at a mounted
# volume (or a synced folder) on a host; it defaults to ./data next to the app.
DATA_DIR = Path(os.environ.get("STUDYMATE_DATA", ROOT / "data")).resolve()
DOCS_DIR = DATA_DIR / "docs"
WS_DIR = DATA_DIR / "workspace"

# Upload ceiling (MB) and how many documents we keep before pruning the oldest.
MAX_MB = _int("STUDYMATE_MAX_MB", 60)
KEEP_DOCS = _int("STUDYMATE_KEEP_DOCS", 200)

# Port: hosts inject $PORT; Hugging Face Spaces expects 7860.
PORT = _int("PORT", 8000)

# CORS: "*" keeps local development and previews painless. Set a comma-separated
# list (e.g. "https://study.example.com") when you host it publicly.
ORIGINS = [o.strip() for o in os.environ.get("STUDYMATE_ORIGINS", "*").split(",") if o.strip()]

# Optional: mirror ./data to an off-host store, because most free hosts have an
# ephemeral disk. Currently supports a Hugging Face dataset repo (free, no card).
BACKUP_REPO = os.environ.get("STUDYMATE_BACKUP_REPO", "").strip()   # e.g. "you/studymate-data"
BACKUP_TOKEN = os.environ.get("HF_TOKEN", "").strip() or os.environ.get("STUDYMATE_BACKUP_TOKEN", "").strip()
BACKUP_SECONDS = _int("STUDYMATE_BACKUP_SECONDS", 90)               # debounce between pushes
BACKUP_ENABLED = _bool("STUDYMATE_BACKUP", bool(BACKUP_REPO and BACKUP_TOKEN))

# A single key the *site owner* provides, so visitors never have to sign up for
# anything. Set the env vars (or paste it in Settings → "Site key") and every
# visitor gets real AI. The key stays server-side; it is never sent to a browser.
SITE_KEY = os.environ.get("STUDYMATE_LLM_KEY", "").strip()
SITE_PROVIDER = os.environ.get("STUDYMATE_LLM_PROVIDER", "gemini").strip().lower()
SITE_MODEL = os.environ.get("STUDYMATE_LLM_MODEL", "").strip()
SITE_BASE = os.environ.get("STUDYMATE_LLM_BASE", "").strip()
# Daily cap per IP so a shared key cannot be drained by one visitor or a bot.
DAILY_LIMIT = _int("STUDYMATE_DAILY_LIMIT", 250)

# Housekeeping
DEBUG = _bool("STUDYMATE_DEBUG", False)


def _parse_backups(raw: str) -> list[dict]:
    """`groq|gsk_xxx|llama-3.3-70b;cerebras|key2` → list of backup keys."""
    out = []
    for piece in (raw or "").split(";"):
        piece = piece.strip()
        if not piece:
            continue
        bits = [b.strip() for b in piece.split("|")]
        if len(bits) < 2 or not bits[0] or not bits[1]:
            continue
        out.append({"provider": bits[0].lower(), "key": bits[1],
                    "model": (bits[2] if len(bits) > 2 and bits[2] else None),
                    "base_url": (bits[3] if len(bits) > 3 and bits[3] else None)})
    return out


BACKUP_KEYS: list[dict] = _parse_backups(os.environ.get("STUDYMATE_LLM_BACKUP", ""))
