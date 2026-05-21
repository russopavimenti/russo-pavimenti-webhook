#!/usr/bin/env python3
"""
Commit the generated carousel slides to the repo, write the carousel post
metadata, and send a Telegram album preview with an approve button.

Runs after gen_carousel.py (reads carousel_out/manifest.json + the PNGs).
Env: GITHUB_TOKEN, GITHUB_REPO_OWNER, GITHUB_REPO_NAME, GITHUB_BRANCH,
     TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
"""
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "carousel_out"

# --- local secrets fallback (for dev) ---
sec = HERE / "secrets.env"
if sec.exists():
    for line in sec.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
REPO_OWNER = os.environ["GITHUB_REPO_OWNER"]
REPO_NAME = os.environ["GITHUB_REPO_NAME"]
BRANCH = os.environ.get("GITHUB_BRANCH", "main")
BOT = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT = int(os.environ["TELEGRAM_CHAT_ID"])

CAPTION = (
    "I 5 passaggi che riportano un pavimento in marmo al suo splendore "
    "originale. ✨\n\n"
    "Ogni fase richiede macchinari specifici, esperienza e la grana giusta "
    "al momento giusto: un lavoro che non si improvvisa.\n\n"
    "Hai un pavimento opaco, graffiato o macchiato? Scrivici in DM o "
    "chiamaci — sopralluogo gratuito ad Alcamo e provincia.\n\n"
    "📍 Russo Pavimenti — lucidatura e restauro marmo\n\n"
    "#russopavimenti #lucidaturamarmo #levigaturamarmo #restauromarmo "
    "#pavimentiinmarmo #marmo #alcamo #trapani #palermo #sicilia"
)


def gh_headers():
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def gh_get_sha(path):
    url = (f"https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}"
           f"/contents/{path}?ref={BRANCH}")
    try:
        with urllib.request.urlopen(
                urllib.request.Request(url, headers=gh_headers()), timeout=15) as r:
            return json.loads(r.read()).get("sha", "")
    except Exception:
        return ""


def gh_write(path, content: bytes, message):
    sha = gh_get_sha(path)
    url = f"https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}/contents/{path}"
    payload = {
        "message": message,
        "content": base64.b64encode(content).decode("ascii"),
        "branch": BRANCH,
    }
    if sha:
        payload["sha"] = sha
    headers = dict(gh_headers())
    headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 method="PUT", headers=headers)
    with urllib.request.urlopen(req, timeout=40) as r:
        r.read()
    print(f"  committed {path}")


def tg(method, payload):
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{BOT}/{method}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=40) as r:
        return json.loads(r.read())


def main():
    manifest_path = OUT / "manifest.json"
    if not manifest_path.exists():
        print("manifest.json missing — run gen_carousel.py first")
        sys.exit(1)
    manifest = json.loads(manifest_path.read_text())
    carousel_id = manifest["carousel_id"]
    slides = manifest["slides"]
    print(f"carousel {carousel_id}: {len(slides)} slides")

    raw_base = (f"https://raw.githubusercontent.com/{REPO_OWNER}/{REPO_NAME}"
                f"/{BRANCH}/posts")
    image_urls = []
    for s in slides:
        png = OUT / s["png"]
        gh_write(f"posts/{s['png']}", png.read_bytes(),
                 f"carousel slide: {s['png']}")
        # Clean URL (no query string) — Instagram rejects query params.
        image_urls.append(f"{raw_base}/{s['png']}")

    # --- carousel post metadata ---
    metadata = {
        "post_id": carousel_id,
        "type": "carousel",
        "image_urls": image_urls,
        "caption": CAPTION,
        "topic": "I passaggi della lucidatura del marmo",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    gh_write(f"posts/{carousel_id}.json",
             (json.dumps(metadata, indent=2, ensure_ascii=False) + "\n").encode(),
             f"carousel meta: {carousel_id}")

    # --- Telegram album preview ---
    cb = int(time.time())  # cache-buster for Telegram (raw URL is stable)
    tg("sendMessage", {
        "chat_id": CHAT,
        "parse_mode": "HTML",
        "text": (f"🎠 <b>NUOVO CAROSELLO</b> — {len(slides)} slide\n"
                 f"<code>{carousel_id}</code>\n\n"
                 "<i>I passaggi della lucidatura del marmo</i>"),
    })
    media = [{"type": "photo", "media": f"{u}?v={cb}"} for u in image_urls]
    r = tg("sendMediaGroup", {"chat_id": CHAT, "media": media})
    print("album:", "OK" if r.get("ok") else r)

    r = tg("sendMessage", {
        "chat_id": CHAT,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "text": "✏️ <b>Caption proposta:</b>\n\n" + CAPTION,
        "reply_markup": {
            "inline_keyboard": [
                [{"text": "✅ Approva e pubblica carosello",
                  "callback_data": f"approve_{carousel_id}"}],
                [
                    {"text": "✏️ Chiedi modifiche",
                     "callback_data": f"modify_{carousel_id}"},
                    {"text": "❌ Scarta",
                     "callback_data": f"discard_{carousel_id}"},
                ],
            ]
        },
    })
    print("buttons:", "OK" if r.get("ok") else r)
    print("DONE")


if __name__ == "__main__":
    main()
