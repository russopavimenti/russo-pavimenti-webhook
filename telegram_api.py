"""Minimal Telegram Bot API wrapper."""
import json
import urllib.request
from urllib.error import HTTPError

from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID


BASE = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"


def call(method: str, payload: dict) -> dict:
    try:
        req = urllib.request.Request(
            f"{BASE}/{method}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    except HTTPError as e:
        try:
            body = json.loads(e.read())
        except Exception:
            body = {"description": str(e)}
        return {"ok": False, "error_code": e.code, **body}
    except Exception as e:
        return {"ok": False, "description": str(e)}


def answer_callback(callback_query_id: str, text: str = "") -> dict:
    return call("answerCallbackQuery", {
        "callback_query_id": callback_query_id,
        "text": text,
    })


def edit_message_buttons(chat_id: int, message_id: int, buttons: list) -> dict:
    """`buttons` is a list of rows; each row is a list of button dicts."""
    return call("editMessageReplyMarkup", {
        "chat_id": chat_id,
        "message_id": message_id,
        "reply_markup": {"inline_keyboard": buttons},
    })


def send_message(text: str, chat_id: int = None, parse_mode: str = "HTML",
                  disable_preview: bool = True, reply_markup: dict = None) -> dict:
    p = {
        "chat_id": chat_id or TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": disable_preview,
    }
    if reply_markup:
        p["reply_markup"] = reply_markup
    return call("sendMessage", p)


def set_webhook(url: str, secret_token: str = None) -> dict:
    p = {"url": url, "allowed_updates": ["callback_query", "message"]}
    if secret_token:
        p["secret_token"] = secret_token
    return call("setWebhook", p)


def delete_webhook() -> dict:
    return call("deleteWebhook", {"drop_pending_updates": False})


def get_webhook_info() -> dict:
    req = urllib.request.Request(f"{BASE}/getWebhookInfo")
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())
