"""
Russo Pavimenti — webhook config.

ALL secrets MUST come from environment variables.
Hardcoded values here are only public identifiers (non-secret).

Local dev: secrets are loaded from `secrets.env` (gitignored).
Render production: secrets are set via Render dashboard env vars.
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).parent

# Load secrets from local .env file if present (dev mode only)
_secrets_file = BASE_DIR / "secrets.env"
if _secrets_file.exists():
    for _line in _secrets_file.read_text().splitlines():
        _line = _line.strip()
        if not _line or _line.startswith("#") or "=" not in _line:
            continue
        _k, _, _v = _line.partition("=")
        os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))


def _require(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if not v:
        raise RuntimeError(
            f"Missing required env var {name!r}. "
            f"Set it in Render env vars (production) or in secrets.env (local)."
        )
    return v


def _optional(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


# === Required secrets ===
TELEGRAM_BOT_TOKEN = _require("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = int(_require("TELEGRAM_CHAT_ID"))
COMPOSIO_API_KEY = _require("COMPOSIO_API_KEY")

# Anthropic API key — optional. When present AND AGENT_AUTONOMOUS_MODE=1,
# the webhook routes text messages to a Claude agent. Otherwise messages
# are just persisted to GitHub inbox/ for Claude-on-Mac to read.
ANTHROPIC_API_KEY = _optional("ANTHROPIC_API_KEY")
ANTHROPIC_MODEL = _optional("ANTHROPIC_MODEL", "claude-sonnet-4-5-20250929")

# Default mode (15/5/2026): autonomous agent OFF. User prefers to handle
# modifications via Claude-on-Mac reading inbox/. Set AGENT_AUTONOMOUS_MODE=1
# to re-enable autonomous agent without removing ANTHROPIC_API_KEY.
AGENT_AUTONOMOUS_MODE = _optional("AGENT_AUTONOMOUS_MODE", "0") == "1"

# Telegram webhook header secret — Telegram sends this in
# X-Telegram-Bot-Api-Secret-Token header on every webhook request.
TELEGRAM_WEBHOOK_SECRET = _require("TELEGRAM_WEBHOOK_SECRET")

# URL path secret (defense in depth + obscurity)
WEBHOOK_PATH_SECRET = _require("WEBHOOK_PATH_SECRET")

# === Required public IDs ===
IG_USER_ID = _require("IG_USER_ID")

# GitHub storage (where post metadata is stored)
GITHUB_REPO_OWNER = _require("GITHUB_REPO_OWNER")
GITHUB_REPO_NAME = _require("GITHUB_REPO_NAME")
GITHUB_BRANCH = _optional("GITHUB_BRANCH", "main")

# Optional: GitHub PAT for private repo access (with repo:contents:read scope).
# Leave empty if the repo is public (we'll use raw.githubusercontent.com).
GITHUB_TOKEN = _optional("GITHUB_TOKEN")

# Composio entity_id (which connected account to use)
COMPOSIO_ENTITY_ID = _optional("COMPOSIO_ENTITY_ID", "default")

# === Server settings ===
WEBHOOK_PORT = int(_optional("PORT", _optional("WEBHOOK_PORT", "5055")))

# === Paths ===
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)
