"""
Inbox persistence — store Telegram text messages from the user
as individual JSON files in the GitHub repo's `inbox/` folder.

Why GitHub: Render free-tier filesystem and memory are ephemeral
(wiped on sleep/restart). GitHub gives us durable storage that
Claude can read from any machine via git pull.

Requires GITHUB_TOKEN with Contents: Read & Write on this repo.
"""
import base64
import json
import logging
import time
import urllib.request
from urllib.error import HTTPError

from config import (
    GITHUB_TOKEN,
    GITHUB_REPO_OWNER,
    GITHUB_REPO_NAME,
    GITHUB_BRANCH,
)

log = logging.getLogger("inbox")


def commit_message(msg_data: dict) -> bool:
    """
    Commit a Telegram message to inbox/<timestamp>_<chat_id>.json.
    Returns True on success, False otherwise.
    """
    if not GITHUB_TOKEN:
        log.warning("inbox commit skipped: GITHUB_TOKEN missing")
        return False

    ts = int(msg_data.get("date") or time.time())
    chat_id = msg_data.get("chat_id", "unknown")
    filename = f"inbox/{ts}_{chat_id}.json"

    body_text = json.dumps(msg_data, indent=2, ensure_ascii=False) + "\n"
    content_b64 = base64.b64encode(body_text.encode("utf-8")).decode("ascii")

    url = (
        f"https://api.github.com/repos/"
        f"{GITHUB_REPO_OWNER}/{GITHUB_REPO_NAME}/contents/{filename}"
    )
    payload = {
        "message": f"inbox: {ts}_{chat_id}",
        "content": content_b64,
        "branch": GITHUB_BRANCH,
    }

    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        method="PUT",
        headers={
            "Authorization": f"Bearer {GITHUB_TOKEN}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            r.read()
        log.info(f"inbox saved: {filename}")
        return True
    except HTTPError as e:
        try:
            body = e.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            body = ""
        log.error(f"inbox commit HTTP {e.code}: {body}")
        return False
    except Exception as e:
        log.error(f"inbox commit exception: {e}")
        return False
