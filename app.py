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
        log.error(f"publish failed: {result.get('error')}")
        tg.edit_message_buttons(
            chat_id, message_id,
            [[{"text": "❌ Pubblicazione fallita", "callback_data": "noop"}]],
        )
        tg.send_message(
            f"❌ <b>Pubblicazione fallita</b> per <code>{post_id}</code>\n\n"
            f"<code>{str(result.get('error'))[:1500]}</code>",
            chat_id=chat_id,
        )
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

@app.route("/", methods=["GET"])
def index():
    return jsonify({
        "service": "russo-pavimenti-webhook",
        "status": "ok",
        "endpoints": ["POST /webhook/<secret>", "GET /health"],
    })


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"ok": True})


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
