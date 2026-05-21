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
import collections
import html
import logging
import threading
import time
from flask import Flask, request, jsonify, abort

import cron_dispatcher
import github_storage
import inbox
import telegram_api as tg
from publisher import publish_image, publish_carousel
from config import (
    WEBHOOK_PATH_SECRET,
    TELEGRAM_WEBHOOK_SECRET,
    WEBHOOK_PORT,
    LOG_DIR,
    ANTHROPIC_API_KEY,
    AGENT_AUTONOMOUS_MODE,
    GITHUB_REPO_OWNER,
    GITHUB_REPO_NAME,
    GITHUB_BRANCH,
    GITHUB_TOKEN,
)

# === Logging ===
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
log = logging.getLogger("webhook")

fh = logging.FileHandler(LOG_DIR / "webhook.log")
fh.setFormatter(logging.Formatter(LOG_FORMAT))
logging.getLogger().addHandler(fh)


app = Flask(__name__)


# === Idempotency: dedup Telegram callbacks ===
# Telegram retries webhook delivery if our handler doesn't ACK within ~10s.
# On Render free tier cold start this happens regularly (~30-60s boot).
# Result: same approve callback delivered 2x → 2 publishes.
#
# We dedup by callback_query.id (Telegram assigns a unique id per click;
# retries use the SAME id) AND by (chat_id, message_id, cb_data) to also
# catch user double-taps. Cache is in-memory (bounded), expires by FIFO.
# In-flight publish tracking ensures concurrent retries within the SAME
# warm container don't both reach _handle_approve.

_CALLBACK_CACHE = collections.OrderedDict()
_INFLIGHT_PUBLISHES = set()
_DEDUP_LOCK = threading.Lock()
_DEDUP_MAX_CACHE = 500
_DEDUP_TTL_SECONDS = 600   # 10 min — long enough to cover any TG retry window


def _is_duplicate_callback(cb_id: str, dedup_key: str) -> bool:
    """Return True if we've already seen this callback. Side effect: mark as seen.

    `dedup_key` is the (chat_id|msg_id|cb_data) composite to also catch
    same-action clicks even if Telegram assigned different ids.
    """
    now = time.time()
    with _DEDUP_LOCK:
        # Expire old entries
        cutoff = now - _DEDUP_TTL_SECONDS
        while _CALLBACK_CACHE:
            oldest_key, oldest_ts = next(iter(_CALLBACK_CACHE.items()))
            if oldest_ts < cutoff:
                _CALLBACK_CACHE.popitem(last=False)
            else:
                break

        if cb_id in _CALLBACK_CACHE or dedup_key in _CALLBACK_CACHE:
            return True

        _CALLBACK_CACHE[cb_id] = now
        _CALLBACK_CACHE[dedup_key] = now
        while len(_CALLBACK_CACHE) > _DEDUP_MAX_CACHE:
            _CALLBACK_CACHE.popitem(last=False)
        return False


def _claim_publish_lock(post_id: str) -> bool:
    """Try to claim the right to publish `post_id`. Returns False if
    another thread already claimed it (concurrent retry in same container)."""
    with _DEDUP_LOCK:
        if post_id in _INFLIGHT_PUBLISHES:
            return False
        _INFLIGHT_PUBLISHES.add(post_id)
        return True


def _release_publish_lock(post_id: str) -> None:
    with _DEDUP_LOCK:
        _INFLIGHT_PUBLISHES.discard(post_id)


# === Callback handlers (run in background threads) ===

def _post_id_from(cb_data: str, prefix: str) -> str:
    return cb_data[len(prefix):] if cb_data.startswith(prefix) else cb_data


def _handle_approve(cb_data: str, chat_id: int, message_id: int) -> None:
    post_id = _post_id_from(cb_data, "approve_")

    # Claim the publish lock for this post_id. If already in-flight in a
    # sibling thread (same container), bail out silently — the other thread
    # is taking care of it.
    if not _claim_publish_lock(post_id):
        log.warning(f"APPROVE skipped (in-flight): post_id={post_id}")
        return

    log.info(f"APPROVE start: post_id={post_id}")

    try:
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

        # Defense in depth: if metadata already has a published media_id
        # (set by a previous successful publish), bail out — this is a
        # cold-start retry from Telegram after the original publish completed.
        if post.get("ig_media_id"):
            log.warning(f"APPROVE skipped (already published): post_id={post_id} "
                        f"media_id={post.get('ig_media_id')}")
            existing_link = post.get("ig_permalink", "")
            if existing_link:
                tg.edit_message_buttons(
                    chat_id, message_id,
                    [[{"text": "✅ Già pubblicato — apri post",
                       "url": existing_link}]],
                )
            return

        is_carousel = (
            post.get("type") == "carousel" or bool(post.get("image_urls"))
        )
        caption = post.get("caption")

        if is_carousel:
            image_urls = post.get("image_urls") or []
            if len(image_urls) < 2 or not caption:
                tg.send_message(
                    f"⚠️ Metadata carosello <code>{post_id}</code> incompleto "
                    f"(servono ≥2 image_urls e una caption). Scrivi a Claude.",
                    chat_id=chat_id,
                )
                return
        else:
            image_url = post.get("image_url")
            if not image_url or not caption:
                tg.send_message(
                    f"⚠️ Metadata di <code>{post_id}</code> incompleto "
                    f"(manca image_url o caption). Scrivi a Claude.",
                    chat_id=chat_id,
                )
                return

        # UI: "publishing..."
        publishing_label = (
            "⏳ Pubblicazione carosello su Instagram..."
            if is_carousel else
            "⏳ Pubblicazione su Instagram in corso..."
        )
        tg.edit_message_buttons(
            chat_id, message_id,
            [[{"text": publishing_label, "callback_data": "noop"}]],
        )

        if is_carousel:
            result = publish_carousel(image_urls, caption)
        else:
            result = publish_image(image_url, caption)

        if not result.get("ok"):
            err_str = str(result.get("error", "unknown error"))
            log.error(f"publish failed: {err_str}")
            tg.edit_message_buttons(
                chat_id, message_id,
                [[{"text": "❌ Pubblicazione fallita", "callback_data": "noop"}]],
            )
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

        # === Success: persist published state back to repo metadata ===
        media_id = result.get("media_id", "")
        permalink = result.get("permalink") or ""
        try:
            post["ig_media_id"] = media_id
            post["ig_permalink"] = permalink
            post["published_at"] = int(time.time())
            github_storage.write_post(post_id, post)
        except Exception as e:
            log.warning(f"failed to write back published state (non-fatal): {e}")

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
            f"🆔 Media ID: <code>{media_id}</code>\n"
            f"🔗 <a href=\"{permalink or 'N/D'}\">Apri su Instagram</a>",
            chat_id=chat_id, disable_preview=False,
        )
    finally:
        _release_publish_lock(post_id)


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

    # Persist to GitHub inbox for auditing / Claude-on-Mac pickup
    persisted = inbox.commit_message(msg_data)

    if ANTHROPIC_API_KEY and AGENT_AUTONOMOUS_MODE:
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
        # Mac-mediated mode: ACK + wait for Claude-on-Mac to read inbox
        preview = (text[:200] + ("..." if len(text) > 200 else ""))
        if persisted:
            tg.send_message(
                "📥 <b>Richiesta ricevuta</b>\n\n"
                "Claude la legge appena sei online sul Mac e ti risponde qui.\n\n"
                f"<i>Anteprima salvata:</i>\n<blockquote>{preview}</blockquote>",
                chat_id=chat_id,
            )
        else:
            tg.send_message(
                "⚠️ Messaggio ricevuto ma non sono riuscito a salvarlo su GitHub. "
                "Riscrivi tra poco.",
                chat_id=chat_id,
            )


# === Flask routes ===

VERSION_MARKER = "v16-carousel"

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


@app.route(f"/cron/tick/{WEBHOOK_PATH_SECRET}", methods=["GET", "POST"])
def cron_tick():
    """External cron entrypoint. Pings this endpoint trigger workflow_dispatch
    if any daily slot is due-but-not-done. Idempotent thanks to in-memory
    cooldown + CAS claim on the GH Actions side.

    Used as a more reliable scheduler than GH Actions free-tier cron, which
    sporadically skips entire days. Suggested ping cadence: every 5-10 min
    from UptimeRobot or cron-job.org.
    """
    try:
        report = cron_dispatcher.tick_once(
            GITHUB_REPO_OWNER, GITHUB_REPO_NAME, GITHUB_BRANCH, GITHUB_TOKEN,
        )
        return jsonify(report)
    except Exception as e:
        log.exception(f"/cron/tick error: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


# Start background dispatcher thread (idempotent)
cron_dispatcher.start(
    GITHUB_REPO_OWNER, GITHUB_REPO_NAME, GITHUB_BRANCH, GITHUB_TOKEN,
)


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


@app.route(f"/debug/{WEBHOOK_PATH_SECRET}/env-check", methods=["GET"])
def debug_env_check():
    """Show which env vars are SET (not values, just presence + length)."""
    import os
    checks = {}
    for k in [
        "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "COMPOSIO_API_KEY",
        "TELEGRAM_WEBHOOK_SECRET", "WEBHOOK_PATH_SECRET",
        "IG_USER_ID", "GITHUB_TOKEN",
        "ANTHROPIC_API_KEY", "ANTHROPIC_MODEL",
        "PEXELS_API_KEY",
    ]:
        v = os.environ.get(k, "")
        checks[k] = {
            "set": bool(v),
            "len": len(v),
            "first6": v[:6] if v else "",
        }
    return jsonify(checks)


@app.route(f"/debug/{WEBHOOK_PATH_SECRET}/test-search", methods=["GET"])
def debug_test_search():
    """Test pexels.search. ALSO direct Pexels API call to see raw response."""
    import os, urllib.request, urllib.parse, json, traceback
    q = request.args.get("q", "marble")

    info = {"query": q}
    key = os.environ.get("PEXELS_API_KEY", "").strip()
    info["key_set"] = bool(key)
    info["key_len"] = len(key)

    # Direct API call so we see status code + raw response
    url = f"https://api.pexels.com/v1/search?query={urllib.parse.quote(q)}&per_page=3"
    try:
        req = urllib.request.Request(url, headers={"Authorization": key})
        with urllib.request.urlopen(req, timeout=15) as r:
            info["http_status"] = r.status
            body = r.read().decode("utf-8")
            info["raw_body_first_500"] = body[:500]
            try:
                d = json.loads(body)
                info["total_results"] = d.get("total_results")
                info["photos_count"] = len(d.get("photos", []))
            except Exception:
                pass
    except urllib.request.HTTPError as e:
        info["http_status"] = e.code
        try:
            info["error_body"] = e.read().decode("utf-8")[:500]
        except Exception:
            info["error_body"] = ""
    except Exception as e:
        info["exception"] = repr(e)
        info["traceback"] = traceback.format_exc()

    # Also run my wrapper for comparison
    import pexels
    try:
        wrapper_results = pexels.search(q, limit=3)
        info["wrapper_count"] = len(wrapper_results)
    except Exception as e:
        info["wrapper_err"] = repr(e)

    return jsonify(info)


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
    log.info(f"callback: {cb_data} chat={chat_id} msg={message_id} cb_id={cb_id}")

    # === Idempotency gate ===
    # Telegram retries this callback if our webhook took >10s to ACK
    # (common during Render free-tier cold starts). Dedup by both the
    # Telegram-assigned callback_query.id AND (chat|msg|data) composite
    # so we also catch user accidental double-taps.
    dedup_key = f"{chat_id}|{message_id}|{cb_data}"
    if _is_duplicate_callback(cb_id, dedup_key):
        log.warning(f"DUPLICATE callback rejected: cb_id={cb_id} key={dedup_key}")
        tg.answer_callback(cb_id, "Già processato")
        return jsonify({"ok": True})

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
