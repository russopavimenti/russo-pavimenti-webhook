"""
Russo Pavimenti — Telegram Webhook server (production).

Deployed on Render free tier. Receives Telegram callback_queries
(button clicks on post previews) and publishes to Instagram via Composio.

Security:
  - URL path includes a random secret (WEBHOOK_PATH_SECRET)
  - Verifies X-Telegram-Bot-Api-Secret-Token header on every request
  - Binds to 0.0.0.0:PORT (Render provides PORT env var)
  - All secrets via env vars; nothing sensitive in repo

Endpoints:
  POST /webhook/<secret>   Telegram webhook receiver
  GET  /health             Liveness probe (for Render health checks)
  GET  /                   Status page
"""
import html
import logging
import threading
import time
from flask import Flask, request, jsonify, abort

import github_storage
import inbox
import telegram_api as tg
from publisher import publish_image
from config import (
    WEBHOOK_PATH_SECRET,
    TELEGRAM_WEBHOOK_SECRET,
    WEBHOOK_PORT,
    LOG_DIR,
    ANTHROPIC_API_KEY,
)

# === Logging ===
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
log = logging.getLogger("webhook")

fh = logging.FileHandler(LOG_DIR / "webhook.log")
fh.setFormatter(logging.Formatter(LOG_FORMAT))
logging.getLogger().addHandler(fh)


app = Flask(__name__)


# === Callback handlers (run in background threads) ===

def _post_id_from(cb_data: str, prefix: str) -> str:
    return cb_data[len(prefix):] if cb_data.startswith(prefix) else cb_data


def _handle_approve(cb_data: str, chat_id: int, message_id: int) -> None:
    post_id = _post_id_from(cb_data, "approve_")
    log.info(f"APPROVE start: post_id={post_id}")

    post = github_storage.get_post(post_id)
    if not post:
        log.error(f"post not found in GitHub: {post_id}")
        tg.edit_message_buttons(
            chat_id, message_id,
            [[{"text": "❌ Metadata mancante — scrivi a Claude",
               "callback_data": "noop"}]],
        )
        tg.send_message(
            f"⚠️ Approvazione ricevuta per <code>{post_id}</code> ma "
            "<b>metadata non trovato su GitHub</b>.\n\n"
            "Probabilmente Claude non ha ancora pushato i file del post. "
            "Riprova dopo che Claude conferma il push, oppure scrivi qui.",
            chat_id=chat_id,
        )
        return

    image_url = post.get("image_url")
    caption = post.get("caption")
    if not image_url or not caption:
        tg.send_message(
            f"⚠️ Metadata di <code>{post_id}</code> incompleto "
            f"(manca image_url o caption). Scrivi a Claude.",
            chat_id=chat_id,
        )
        return

    # UI: "publishing..."
    tg.edit_message_buttons(
        chat_id, message_id,
        [[{"text": "⏳ Pubblicazione su Instagram in corso...",
           "callback_data": "noop"}]],
    )

    result = publish_image(image_url, caption)

    if not result.get("ok"):
        err_str = str(result.get("error", "unknown error"))
        log.error(f"publish failed: {err_str}")
        tg.edit_message_buttons(
            chat_id, message_id,
            [[{"text": "❌ Pubblicazione fallita", "callback_data": "noop"}]],
        )
        # First attempt: HTML-escaped detail. Fallback: plain text.
        err_escaped = html.escape(err_str[:1500])
        post_id_escaped = html.escape(post_id)
        r = tg.send_message(
            f"❌ <b>Pubblicazione fallita</b> per <code>{post_id_escaped}</code>\n\n"
            f"<code>{err_escaped}</code>",
            chat_id=chat_id,
        )
        if not r.get("ok"):
            log.warning(f"telegram html send failed: {r}; retrying plain")
            tg.call("sendMessage", {
                "chat_id": chat_id,
                "text": f"❌ Pubblicazione fallita per {post_id}\n\n{err_str[:1500]}",
            })
        return

    permalink = result.get("permalink") or ""
    if permalink:
        tg.edit_message_buttons(
            chat_id, message_id,
            [[{"text": "✅ Pubblicato — apri post", "url": permalink}]],
        )
    else:
        tg.edit_message_buttons(
            chat_id, message_id,
            [[{"text": "✅ Pubblicato", "callback_data": "noop"}]],
        )

    topic = post.get("topic") or post_id
    tg.send_message(
        f"🟢 <b>POST PUBBLICATO</b>\n\n"
        f"📌 <i>{topic}</i>\n"
        f"🆔 Media ID: <code>{result['media_id']}</code>\n"
        f"🔗 <a href=\"{permalink or 'N/D'}\">Apri su Instagram</a>",
        chat_id=chat_id, disable_preview=False,
    )


def _handle_modify(cb_data: str, chat_id: int, message_id: int) -> None:
    post_id = _post_id_from(cb_data, "modify_")
    tg.edit_message_buttons(
        chat_id, message_id,
        [[{"text": "✏️ Modifica richiesta — scrivi a Claude",
           "callback_data": "noop"}]],
    )
    tg.send_message(
        f"✏️ <b>Modifica richiesta</b> per <code>{post_id}</code>\n\n"
        "Scrivi a Claude cosa cambiare (testo, foto, layout) — "
        "riceverai una nuova preview.",
        chat_id=chat_id,
    )


def _handle_discard(cb_data: str, chat_id: int, message_id: int) -> None:
    post_id = _post_id_from(cb_data, "discard_")
    tg.edit_message_buttons(
        chat_id, message_id,
        [[{"text": "❌ Scartato", "callback_data": "noop"}]],
    )
    tg.send_message(
        f"❌ Post <code>{post_id}</code> scartato — non sarà pubblicato.",
        chat_id=chat_id,
    )


def _handle_text_message(message: dict) -> None:
    """User sent a text message to the bot. Persist to GitHub inbox + echo."""
    chat_id = message.get("chat", {}).get("id")
    user = message.get("from", {}) or {}
    text = message.get("text") or message.get("caption") or ""
    if not text:
        return  # ignore stickers, photos w/o caption, etc.

    msg_data = {
        "telegram_message_id": message.get("message_id"),
        "chat_id": chat_id,
        "user_id": user.get("id"),
        "username": user.get("username"),
        "first_name": user.get("first_name"),
        "text": text,
        "date": message.get("date"),
        "received_at": int(time.time()),
    }

    # Persist to GitHub inbox for auditing / fallback
    inbox.commit_message(msg_data)

    if ANTHROPIC_API_KEY:
        # Autonomous mode: Claude agent processes the request directly
        tg.send_message(
            "👀 <i>Sto pensando...</i>",
            chat_id=chat_id,
        )
        try:
            import claude_agent
            claude_agent.run(
                user_text=text,
                user_name=user.get("first_name") or "Nino",
            )
        except Exception as e:
            log.exception("agent run failed")
            tg.send_message(
                f"⚠️ Errore agent: <code>{str(e)[:300]}</code>",
                chat_id=chat_id,
            )
    else:
        # Fallback: just ACK and wait for Claude on Mac to read inbox
        preview = (text[:200] + ("..." if len(text) > 200 else ""))
        tg.send_message(
            "📥 <b>Richiesta ricevuta</b>\n\n"
            "L'assistente Mac la processerà al prossimo accesso.\n\n"
            f"<i>Anteprima registrata:</i>\n<blockquote>{preview}</blockquote>",
            chat_id=chat_id,
        )


# === Flask routes ===

VERSION_MARKER = "v9-pexels-official-api"

@app.route("/", methods=["GET"])
def index():
    return jsonify({
        "service": "russo-pavimenti-webhook",
        "status": "ok",
        "version": VERSION_MARKER,
        "endpoints": ["POST /webhook/<secret>", "GET /health"],
    })


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"ok": True})


@app.route(f"/debug/{WEBHOOK_PATH_SECRET}/logs", methods=["GET"])
def debug_logs():
    """Return last 300 lines of webhook log for debugging. Protected by path secret."""
    log_file = LOG_DIR / "webhook.log"
    if not log_file.exists():
        return jsonify({"error": "no log file yet"}), 404
    try:
        with open(log_file, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        return jsonify({"lines": lines[-300:], "total_lines": len(lines)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route(f"/debug/{WEBHOOK_PATH_SECRET}/test-search", methods=["GET"])
def debug_test_search():
    """Test pexels.search to confirm whether scraping works from Render IP."""
    import pexels
    q = request.args.get("q", "marble")
    try:
        results = pexels.search(q, limit=6)
        return jsonify({"query": q, "count": len(results), "results": results})
    except Exception as e:
        import traceback
        return jsonify({
            "query": q, "error": repr(e),
            "traceback": traceback.format_exc(),
        }), 500


@app.route(f"/debug/{WEBHOOK_PATH_SECRET}/connections", methods=["GET"])
def debug_connections():
    """List Composio connected accounts to find the right user_id for Instagram."""
    import traceback
    info = {}
    try:
        from composio import Composio
        from config import COMPOSIO_API_KEY
        client = Composio(api_key=COMPOSIO_API_KEY)

        # Try multiple list endpoints in the v3 SDK
        for attr in ("connected_accounts", "connections", "accounts"):
            if hasattr(client, attr):
                info[f"has_{attr}"] = True
                obj = getattr(client, attr)
                for method in ("list", "get_all", "all"):
                    if hasattr(obj, method):
                        try:
                            result = getattr(obj, method)()
                            # Handle Pydantic models
                            if hasattr(result, "model_dump"):
                                result = result.model_dump()
                            info[f"{attr}.{method}"] = str(result)[:3000]
                            break
                        except Exception as e:
                            info[f"{attr}.{method}_err"] = repr(e)
                break

        # Also list via direct HTTP API
        import urllib.request, json as j
        from config import COMPOSIO_API_KEY
        try:
            req = urllib.request.Request(
                "https://backend.composio.dev/api/v3/connected_accounts",
                headers={"x-api-key": COMPOSIO_API_KEY},
            )
            with urllib.request.urlopen(req, timeout=15) as r:
                data = j.loads(r.read())
            info["http_v3_response"] = data
        except Exception as e:
            info["http_v3_err"] = repr(e)

        return jsonify(info)
    except Exception as e:
        return jsonify({
            "error": repr(e),
            "traceback": traceback.format_exc(),
        }), 500


@app.route(f"/debug/{WEBHOOK_PATH_SECRET}/test-publish", methods=["GET"])
def debug_test_publish():
    """Run the actual publish flow with the queryparam post_id and return
    the result + full traceback via HTTP. For remote diagnosis."""
    import traceback
    post_id = request.args.get("post_id", "daily_02_macchie_vino")
    info = {"post_id": post_id, "stage": "init"}

    try:
        info["stage"] = "load_post"
        post = github_storage.get_post(post_id)
        if not post:
            return jsonify({**info, "error": "post not found"}), 404

        info["image_url"] = post.get("image_url")
        info["caption_len"] = len(post.get("caption", ""))

        info["stage"] = "import_composio"
        from composio import Composio
        info["composio_imported"] = True

        info["stage"] = "client_init"
        from config import COMPOSIO_API_KEY, IG_USER_ID, COMPOSIO_ENTITY_ID
        client = Composio(api_key=COMPOSIO_API_KEY)
        info["client_type"] = type(client).__name__
        info["has_actions"] = hasattr(client, "actions")
        info["has_get_entity"] = hasattr(client, "get_entity")
        info["has_tools"] = hasattr(client, "tools")

        # try to resolve Action enum
        info["stage"] = "resolve_action"
        try:
            from composio import Action
            info["has_Action"] = True
            info["Action_type"] = type(Action).__name__
            try:
                a = Action["INSTAGRAM_POST_IG_USER_MEDIA"]
                info["action_bracket"] = repr(a)
            except Exception as e:
                info["action_bracket_err"] = repr(e)
            try:
                a = Action("INSTAGRAM_POST_IG_USER_MEDIA")
                info["action_call"] = repr(a)
            except Exception as e:
                info["action_call_err"] = repr(e)
            try:
                info["action_no_auth"] = getattr(a, "no_auth", "<not present>")
            except Exception as e:
                info["action_no_auth_err"] = repr(e)
        except ImportError as e:
            info["Action_import_err"] = repr(e)

        # Attempt actual publish
        info["stage"] = "publish_image"
        result = publish_image(
            post["image_url"], post["caption"], max_wait_seconds=60,
        )
        info["publish_result"] = result

        return jsonify(info)

    except Exception as e:
        info["exception"] = repr(e)
        info["traceback"] = traceback.format_exc()
        return jsonify(info), 500


@app.route(f"/webhook/{WEBHOOK_PATH_SECRET}", methods=["POST"])
def webhook():
    # Defense layer 1: header secret (Telegram signs every webhook)
    header_secret = request.headers.get(
        "X-Telegram-Bot-Api-Secret-Token", ""
    )
    if header_secret != TELEGRAM_WEBHOOK_SECRET:
        log.warning(f"rejected: bad/missing header secret "
                    f"(remote={request.remote_addr})")
        abort(403)

    update = request.get_json(silent=True) or {}
    log.info(f"update: keys={list(update.keys())}")

    # === Text message handler (user typing to the bot) ===
    msg = update.get("message")
    if msg and (msg.get("text") or msg.get("caption")):
        threading.Thread(
            target=_handle_text_message, args=(msg,), daemon=True,
        ).start()
        return jsonify({"ok": True})

    # === Callback query handler (button click) ===
    cbq = update.get("callback_query")
    if not cbq:
        return jsonify({"ok": True})

    cb_id = cbq.get("id")
    cb_data = cbq.get("data", "")
    msg = cbq.get("message", {})
    chat_id = msg.get("chat", {}).get("id")
    message_id = msg.get("message_id")
    log.info(f"callback: {cb_data} chat={chat_id} msg={message_id}")

    # ACK first so Telegram doesn't retry / user doesn't see spinner
    if cb_data.startswith("approve_"):
        tg.answer_callback(cb_id, "Approvato! Pubblico ora...")
        threading.Thread(
            target=_handle_approve,
            args=(cb_data, chat_id, message_id),
            daemon=True,
        ).start()
    elif cb_data.startswith("modify_"):
        tg.answer_callback(cb_id, "Modifica richiesta registrata")
        threading.Thread(
            target=_handle_modify,
            args=(cb_data, chat_id, message_id),
            daemon=True,
        ).start()
    elif cb_data.startswith("discard_"):
        tg.answer_callback(cb_id, "Scartato")
        threading.Thread(
            target=_handle_discard,
            args=(cb_data, chat_id, message_id),
            daemon=True,
        ).start()
    elif cb_data == "noop":
        tg.answer_callback(cb_id, "")
    else:
        tg.answer_callback(cb_id, "callback non riconosciuto")

    return jsonify({"ok": True})


if __name__ == "__main__":
    log.info(f"starting webhook on port {WEBHOOK_PORT}")
    app.run(host="0.0.0.0", port=WEBHOOK_PORT, debug=False)
