#!/usr/bin/env python3
"""
Daily Post Cron Job — generates 1 post and sends preview to Telegram.

Run by Render cron at 3 scheduled times per day:
  - 08:00 Italy (06:00 UTC summer / 07:00 UTC winter)
  - 12:30 Italy (10:30 UTC summer / 11:30 UTC winter)
  - 17:30 Italy (15:30 UTC summer / 16:30 UTC winter)

Each invocation:
  1. Loads daily_topics.json (12 pre-written topics, expandable)
  2. Determines next topic to use (rotation tracked via daily_state.json)
  3. Searches Pexels for a relevant photo
  4. Renders the post PNG via renderer.py
  5. Commits posts/<post_id>.{png,json} + daily_state.json to repo
  6. Sends preview to Telegram with approve/modify/discard buttons

Env vars required (inherits from webhook service):
  - TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
  - GITHUB_TOKEN, GITHUB_REPO_OWNER, GITHUB_REPO_NAME, GITHUB_BRANCH
  - PEXELS_API_KEY

Optional:
  - DAILY_SLOT: "morning" | "lunch" | "evening" (cosmetic, used in post_id)
"""
import base64
import json
import logging
import os
import random
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

# Make renderer and pexels importable
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("daily-cron")


# === Env loading (also picks up local secrets.env for dev) ===
def load_local_secrets() -> None:
    f = HERE / "secrets.env"
    if not f.exists():
        return
    for line in f.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_local_secrets()

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = int(os.environ["TELEGRAM_CHAT_ID"])
GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
GITHUB_REPO_OWNER = os.environ["GITHUB_REPO_OWNER"]
GITHUB_REPO_NAME = os.environ["GITHUB_REPO_NAME"]
GITHUB_BRANCH = os.environ.get("GITHUB_BRANCH", "main")
PEXELS_API_KEY = os.environ.get("PEXELS_API_KEY", "")

DAILY_SLOT = os.environ.get("DAILY_SLOT", "auto")  # morning / lunch / evening / auto

TOPICS_FILE = HERE / "daily_topics.json"
STATE_FILE_REPO = "daily_state.json"   # committed to repo so all cron jobs share state
COMMON_HASHTAGS = (
    "#russopavimenti #lucidaturamarmo #levigaturamarmo "
    "#restauromarmo #marmocemento #pavimentiinmarmo "
    "#alcamo #trapani #palermo #sicilia"
)


# === GitHub Contents API helpers ===
def _gh_headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def gh_read(path: str) -> bytes:
    url = (
        f"https://api.github.com/repos/{GITHUB_REPO_OWNER}/{GITHUB_REPO_NAME}"
        f"/contents/{path}?ref={GITHUB_BRANCH}"
    )
    req = urllib.request.Request(url, headers=_gh_headers())
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            meta = json.loads(r.read())
        if meta.get("encoding") == "base64":
            return base64.b64decode(meta["content"])
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return b""
        raise
    return b""


def gh_get_sha(path: str) -> str:
    url = (
        f"https://api.github.com/repos/{GITHUB_REPO_OWNER}/{GITHUB_REPO_NAME}"
        f"/contents/{path}?ref={GITHUB_BRANCH}"
    )
    req = urllib.request.Request(url, headers=_gh_headers())
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read()).get("sha", "")
    except Exception:
        return ""


def gh_write(path: str, content: bytes, message: str) -> bool:
    sha = gh_get_sha(path)
    url = (
        f"https://api.github.com/repos/{GITHUB_REPO_OWNER}/{GITHUB_REPO_NAME}"
        f"/contents/{path}"
    )
    payload: Dict[str, Any] = {
        "message": message,
        "content": base64.b64encode(content).decode("ascii"),
        "branch": GITHUB_BRANCH,
    }
    if sha:
        payload["sha"] = sha
    headers = dict(_gh_headers())
    headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        method="PUT", headers=headers,
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            r.read()
        log.info(f"gh_write OK: {path}")
        return True
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:300]
        log.error(f"gh_write {path} HTTP {e.code}: {body}")
        return False


# === State (rotation tracker, stored in repo) ===
def load_state() -> Dict[str, Any]:
    raw = gh_read(STATE_FILE_REPO)
    if not raw:
        return {"next_index": 0, "history": []}
    try:
        return json.loads(raw)
    except Exception:
        return {"next_index": 0, "history": []}


def save_state(state: Dict[str, Any]) -> None:
    body = (json.dumps(state, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    gh_write(STATE_FILE_REPO, body, f"daily_state: bump to index {state.get('next_index')}")


# === Topics loading ===
def load_topics() -> List[Dict[str, Any]]:
    raw = TOPICS_FILE.read_text()
    data = json.loads(raw)
    return data["topics"]


def pick_next_topic(topics: List[Dict[str, Any]], state: Dict[str, Any]) -> Dict[str, Any]:
    idx = state.get("next_index", 0) % len(topics)
    return topics[idx]


# === Pexels search/download (via API with UA) ===
PEXELS_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"


def pexels_search_first(queries: List[str], limit: int = 8) -> str:
    """Return the URL of the first usable photo from any of the queries."""
    import urllib.parse as up
    for q in queries:
        url = f"https://api.pexels.com/v1/search?query={up.quote(q)}&per_page={limit}"
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": PEXELS_API_KEY,
                "User-Agent": PEXELS_UA,
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                data = json.loads(r.read())
        except Exception as e:
            log.warning(f"pexels query {q!r} failed: {e}")
            continue
        for photo in data.get("photos", []):
            src = photo.get("src", {})
            u = src.get("large") or src.get("medium") or src.get("original")
            if u:
                log.info(f"picked photo id={photo.get('id')} from query={q!r}")
                return u
    return ""


def pexels_download(url: str, dest: str) -> bool:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": PEXELS_UA})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = r.read()
        Path(dest).write_bytes(data)
        return True
    except Exception as e:
        log.warning(f"download {url} failed: {e}")
        return False


# === Telegram ===
def tg_call(method: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def tg_send_preview(post_id: str, raw_url: str, caption: str, slot: str) -> None:
    tg_call("sendMessage", {
        "chat_id": TELEGRAM_CHAT_ID,
        "parse_mode": "HTML",
        "text": (
            f"📅 <b>NUOVO POST — slot {slot}</b>\n"
            f"<code>{post_id}</code>"
        ),
    })
    tg_call("sendPhoto", {
        "chat_id": TELEGRAM_CHAT_ID,
        "photo": raw_url,
        "caption": "🖼 Anteprima",
        "parse_mode": "HTML",
    })
    tg_call("sendMessage", {
        "chat_id": TELEGRAM_CHAT_ID,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "text": "✏️ <b>Caption proposta:</b>\n\n" + caption,
        "reply_markup": {
            "inline_keyboard": [
                [{"text": "✅ Approva e pubblica", "callback_data": f"approve_{post_id}"}],
                [
                    {"text": "✏️ Chiedi modifiche", "callback_data": f"modify_{post_id}"},
                    {"text": "❌ Scarta", "callback_data": f"discard_{post_id}"},
                ],
            ]
        },
    })


# === Main flow ===
def determine_slot() -> str:
    """If env DAILY_SLOT not set, derive from current Italy hour."""
    if DAILY_SLOT and DAILY_SLOT != "auto":
        return DAILY_SLOT
    # Italy = UTC+1/+2. Use UTC hour and approximate slot mapping.
    now_utc = datetime.now(timezone.utc)
    h = now_utc.hour
    if h < 9:    # 06-08 UTC ≈ 08-10 Italy
        return "morning"
    if h < 13:   # 09-12 UTC ≈ 11-14 Italy
        return "lunch"
    return "evening"


def main() -> int:
    slot = determine_slot()
    log.info(f"=== daily post cron START (slot={slot}) ===")

    topics = load_topics()
    state = load_state()

    # === Idempotency gate ===
    # Multiple redundant cron triggers fire per slot (workflow has 3 each)
    # to compensate for GitHub Actions free-tier delays/skips. Check the
    # history: if today's UTC date + this slot already produced a post,
    # exit cleanly so we don't double-generate.
    today_utc = datetime.now(timezone.utc).strftime("%Y%m%d")
    history = state.get("history", []) or []
    for entry in history:
        post_id_existing = entry.get("post_id", "")
        if entry.get("slot") == slot and post_id_existing.startswith(f"daily_{today_utc}_{slot}_"):
            log.info(
                f"IDEMPOTENT skip: post for {today_utc}+{slot} already exists "
                f"({post_id_existing}). Exit 0."
            )
            return 0

    topic = pick_next_topic(topics, state)
    log.info(f"picked topic[{state.get('next_index', 0)}]: {topic['id']} ({topic['topic']})")

    today = today_utc
    post_id = f"daily_{today}_{slot}_{topic['id']}"

    # 1. Pick photo from Pexels
    queries = topic.get("pexels_queries") or [topic["topic"]]
    photo_url = pexels_search_first(queries)
    if not photo_url:
        log.error("no photo found, abort")
        tg_call("sendMessage", {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": f"⚠️ Daily cron {slot}: nessuna foto trovata per topic {topic['id']!r}. Vado avanti senza generare.",
        })
        return 1

    # 2. Download photo
    tmp_photo = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False).name
    if not pexels_download(photo_url, tmp_photo):
        log.error("download failed")
        return 1

    # 3. Render
    import renderer
    tmp_out = tempfile.NamedTemporaryFile(suffix=".png", delete=False).name
    renderer.render(
        photo_path=tmp_photo,
        line1=topic["hook_line1"],
        line2=topic["hook_line2"],
        output_path=tmp_out,
        overlay_alpha=topic.get("overlay_alpha", 0.30),
        backdrop_intensity=130,
    )

    # 4. Build caption
    caption = (
        topic.get("caption_body", "")
        + "\n\n"
        + COMMON_HASHTAGS
        + " " + topic.get("extra_hashtags", "")
    )

    # 5. Commit PNG + JSON metadata
    png_bytes = Path(tmp_out).read_bytes()
    if not gh_write(f"posts/{post_id}.png", png_bytes, f"daily: {post_id}"):
        return 1

    image_url = (
        f"https://raw.githubusercontent.com/"
        f"{GITHUB_REPO_OWNER}/{GITHUB_REPO_NAME}/{GITHUB_BRANCH}/"
        f"posts/{post_id}.png"
    )
    metadata = {
        "post_id": post_id,
        "image_url": image_url,
        "caption": caption,
        "topic": topic["topic"],
        "topic_id": topic["id"],
        "slot": slot,
        "hook_line1": topic["hook_line1"],
        "hook_line2": topic["hook_line2"],
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_photo_url": photo_url,
    }
    meta_bytes = (json.dumps(metadata, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    if not gh_write(f"posts/{post_id}.json", meta_bytes, f"daily meta: {post_id}"):
        return 1

    # 6. Send Telegram preview with cache-buster
    tg_send_preview(post_id, image_url + f"?v={int(time.time())}", caption, slot)

    # 7. Advance state
    state["next_index"] = (state.get("next_index", 0) + 1) % len(topics)
    state.setdefault("history", []).append({
        "post_id": post_id,
        "topic_id": topic["id"],
        "slot": slot,
        "at": datetime.now(timezone.utc).isoformat(),
    })
    state["history"] = state["history"][-50:]   # keep last 50
    save_state(state)

    # Cleanup
    for p in (tmp_photo, tmp_out):
        try:
            os.unlink(p)
        except OSError:
            pass

    log.info(f"=== daily cron DONE: {post_id} ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
