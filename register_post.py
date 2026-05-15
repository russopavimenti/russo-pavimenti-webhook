"""
Mac-side helper used by Claude to register a new post for approval.

Usage:
    python3 register_post.py \
        --post-id daily_02_macchie_vino \
        --png /path/to/local/render.png \
        --caption-file /path/to/caption.txt \
        --topic "Macchie di vino sul marmo" \
        --send-telegram-preview

Effects:
    1. Copies PNG into posts/<post_id>.png
    2. Writes posts/<post_id>.json with public raw GitHub image_url
    3. git add + commit + push to GitHub
    4. (optional) Sends Telegram preview message with Approve/Modify/Discard

The Render webhook reads the JSON from GitHub raw on every callback,
so no further sync is needed.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

THIS_DIR = Path(__file__).resolve().parent
POSTS_DIR = THIS_DIR / "posts"


def _load_secrets():
    f = THIS_DIR / "secrets.env"
    if not f.exists():
        return
    for line in f.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _run(*cmd, cwd=None):
    print(f"$ {' '.join(cmd)}")
    subprocess.check_call(cmd, cwd=cwd or THIS_DIR)


def register(
    post_id: str, png_path: Path, caption: str, topic: str,
    hook_line1: str = "", hook_line2: str = "",
) -> dict:
    POSTS_DIR.mkdir(exist_ok=True)
    safe = post_id.replace("/", "_").replace("..", "_")

    target_png = POSTS_DIR / f"{safe}.png"
    target_json = POSTS_DIR / f"{safe}.json"

    shutil.copyfile(png_path, target_png)

    owner = os.environ["GITHUB_REPO_OWNER"]
    repo = os.environ["GITHUB_REPO_NAME"]
    branch = os.environ.get("GITHUB_BRANCH", "main")
    image_url = (
        f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/"
        f"posts/{safe}.png"
    )

    metadata = {
        "post_id": safe,
        "image_url": image_url,
        "caption": caption,
        "topic": topic,
        "hook_line1": hook_line1,
        "hook_line2": hook_line2,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    target_json.write_text(
        json.dumps(metadata, indent=2, ensure_ascii=False) + "\n"
    )

    # Commit + push
    _run("git", "add", str(target_png), str(target_json))
    _run("git", "commit", "-m", f"post: {safe}")
    _run("git", "push", "origin", branch)

    return metadata


def send_telegram_preview(post_id: str, image_url: str, caption: str) -> None:
    """Send Telegram preview with Approve/Modify/Discard buttons."""
    import urllib.request

    bot = os.environ["TELEGRAM_BOT_TOKEN"]
    chat = int(os.environ["TELEGRAM_CHAT_ID"])

    def tg(method, payload):
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{bot}/{method}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())

    # 1. Photo + minimal caption
    tg("sendPhoto", {
        "chat_id": chat,
        "photo": image_url,
        "caption": f"🖼 Anteprima · post_id: <code>{post_id}</code>",
        "parse_mode": "HTML",
    })

    # 2. Full caption + buttons (separato perché caption Telegram max 1024)
    r = tg("sendMessage", {
        "chat_id": chat,
        "text": "✏️ <b>Caption proposta:</b>\n\n" + caption,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "reply_markup": {
            "inline_keyboard": [
                [{"text": "✅ Approva e pubblica",
                  "callback_data": f"approve_{post_id}"}],
                [{"text": "✏️ Chiedi modifiche",
                  "callback_data": f"modify_{post_id}"},
                 {"text": "❌ Scarta",
                  "callback_data": f"discard_{post_id}"}],
            ]
        },
    })
    print("Telegram preview:", "OK" if r.get("ok") else r)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--post-id", required=True)
    p.add_argument("--png", required=True, type=Path)
    p.add_argument("--caption-file", required=True, type=Path)
    p.add_argument("--topic", default="")
    p.add_argument("--hook-line1", default="", help="Prima riga del testo nel post (bianca)")
    p.add_argument("--hook-line2", default="", help="Seconda riga del testo (lime)")
    p.add_argument("--send-telegram-preview", action="store_true")
    a = p.parse_args()

    _load_secrets()

    caption = a.caption_file.read_text().rstrip()
    meta = register(
        a.post_id, a.png, caption, a.topic,
        hook_line1=a.hook_line1, hook_line2=a.hook_line2,
    )

    print(f"\n✅ Registered: posts/{a.post_id}.json")
    print(f"   image_url: {meta['image_url']}")

    if a.send_telegram_preview:
        send_telegram_preview(a.post_id, meta["image_url"], caption)


if __name__ == "__main__":
    main()
