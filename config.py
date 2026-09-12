"""Project configuration loaded from environment variables (and local .env)."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

# Bothost keeps /app/data between restarts/deploys. Locally we keep ./data.
DATA_DIR = Path(os.getenv("DATA_DIR") or (BASE_DIR / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)


def _int(name, default):
    try:
        return int(str(os.getenv(name, default)).strip())
    except (TypeError, ValueError):
        return default


def _float(name, default):
    try:
        return float(str(os.getenv(name, default)).strip().replace(",", "."))
    except (TypeError, ValueError):
        return default


def _bool(name, default):
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on", "да")


# ---------- Telegram ----------
BOT_TOKEN = (os.getenv("BOT_TOKEN") or "").strip()
ADMIN_ID = _int("ADMIN_ID", 0)
BOT_NAME = (os.getenv("BOT_NAME") or "Нейро").strip()

# ---------- Storage ----------
# On hosting use the shared PostgreSQL database. Tables are isolated in their
# own schema so this bot can share DATABASE_URL with Emerald safely.
DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
AIBOT_DB_SCHEMA = (os.getenv("AIBOT_DB_SCHEMA") or "aibot").strip()
# SQLite remains a local-development fallback when DATABASE_URL is absent.
DB_PATH = DATA_DIR / (os.getenv("DB_NAME") or "brain.db")

# ---------- Teacher ----------
TEACHER = (os.getenv("TEACHER") or "web").strip().lower()

GEMINI_WEB_URL = (os.getenv("GEMINI_WEB_URL") or "https://gemini.google.com/app").strip()
GEMINI_WEB_HEADLESS = _bool("GEMINI_WEB_HEADLESS", False)
GEMINI_WEB_TIMEOUT = _int("GEMINI_WEB_TIMEOUT", 180)
GEMINI_WEB_NEW_CHAT = _bool("GEMINI_WEB_NEW_CHAT", True)
GEMINI_PROFILE_DIR = DATA_DIR / "gemini_profile"

# Bothost publishes the noVNC port through its assigned DOMAIN. This value is
# only informational; the browser itself is launched by Playwright in DISPLAY.
_domain = (os.getenv("DOMAIN") or "").strip().strip("/")
REMOTE_BROWSER_URL = (os.getenv("REMOTE_BROWSER_URL") or (
    ("https://" + _domain + "/vnc.html?autoconnect=true&resize=remote") if _domain else ""
)).strip()

GEMINI_API_KEY = (os.getenv("GEMINI_API_KEY") or "").strip()
GEMINI_MODEL = (os.getenv("GEMINI_MODEL") or "gemini-2.5-flash").strip()
GEMINI_API_TIMEOUT = _int("GEMINI_API_TIMEOUT", 120)

# ---------- Brain ----------
SIM_THRESHOLD = _float("SIM_THRESHOLD", 0.88)
CONTEXT_TURNS = _int("CONTEXT_TURNS", 12)

# ---------- Media ----------
MAX_MEDIA_MB = min(_int("MAX_MEDIA_MB", 20), 20)
MEDIA_DIR = DATA_DIR / "tmp"
MEDIA_DIR.mkdir(parents=True, exist_ok=True)
MAX_ALBUM = _int("MAX_ALBUM", 6)
ALBUM_WAIT = _float("ALBUM_WAIT", 1.2)

# ---------- Limits ----------
MAX_INPUT_CHARS = _int("MAX_INPUT_CHARS", 4000)
TG_CHUNK = 3400
