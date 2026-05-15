"""
GitHub-backed post metadata storage.

Why: Render free tier has no persistent disk. Service spins down
after inactivity and filesystem is wiped on cold start. So we use the
GitHub repo as the source of truth for post metadata.

Flow:
  1. Mac side (where Claude renders posts) writes
     `posts/<post_id>.json` and `posts/<post_id>.png` to the repo,
     commits, pushes.
  2. Render webhook reads via:
       - raw.githubusercontent.com (public repos, no auth)
       - GitHub Contents API + PAT (private repos)

Status updates after publish are NOT written back to GitHub by the
webhook (to avoid the webhook needing write access). The webhook
optionally posts a status message to Telegram instead.

Metadata JSON schema:
{
  "post_id": "daily_01_sporco_continuo",
  "image_url": "https://raw.githubusercontent.com/<owner>/<repo>/<branch>/posts/daily_01_sporco_continuo.png",
  "caption": "<full IG caption text>",
  "topic": "<short topic description>",
  "created_at": "<ISO timestamp>"
}
"""
import json
import logging
import base64
import urllib.request
from urllib.error import HTTPError
from typing import Optional

from config import (
    GITHUB_REPO_OWNER,
    GITHUB_REPO_NAME,
    GITHUB_BRANCH,
    GITHUB_TOKEN,
)

log = logging.getLogger("github_storage")

_RAW_BASE = (
    f"https://raw.githubusercontent.com/"
    f"{GITHUB_REPO_OWNER}/{GITHUB_REPO_NAME}/{GITHUB_BRANCH}"
)
_API_BASE = (
    f"https://api.github.com/repos/"
    f"{GITHUB_REPO_OWNER}/{GITHUB_REPO_NAME}/contents"
)


def _fetch_raw(path: str) -> Optional[bytes]:
    """Fetch file content using GitHub raw URL (public repos only)."""
    url = f"{_RAW_BASE}/{path}"
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            return r.read()
    except HTTPError as e:
        if e.code == 404:
            return None
        raise
    except Exception as e:
        log.warning(f"raw fetch failed for {path}: {e}")
        return None


def _fetch_api(path: str) -> Optional[bytes]:
    """Fetch file content via GitHub Contents API (works for private repos)."""
    if not GITHUB_TOKEN:
        return None
    url = f"{_API_BASE}/{path}?ref={GITHUB_BRANCH}"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {GITHUB_TOKEN}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            meta = json.loads(r.read())
        if meta.get("encoding") == "base64":
            return base64.b64decode(meta["content"])
        return None
    except HTTPError as e:
        if e.code == 404:
            return None
        log.warning(f"api fetch failed for {path}: {e.code}")
        return None
    except Exception as e:
        log.warning(f"api fetch failed for {path}: {e}")
        return None


def fetch_file(path: str) -> Optional[bytes]:
    """
    Try API+PAT first (works for private + public), fall back to raw URL.
    """
    if GITHUB_TOKEN:
        data = _fetch_api(path)
        if data is not None:
            return data
    return _fetch_raw(path)


def get_post(post_id: str) -> Optional[dict]:
    """Get a post's metadata JSON by post_id."""
    safe_id = post_id.replace("/", "_").replace("..", "_")
    raw = fetch_file(f"posts/{safe_id}.json")
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        log.error(f"invalid JSON in posts/{safe_id}.json: {e}")
        return None
