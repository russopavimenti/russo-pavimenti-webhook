"""
Tool implementations for the Claude agent.

Each tool runs server-side on Render. The agent can chain them:
  search_pexels → choose URL → apply_image_change → reply_to_user
"""
import base64
import json
import logging
import os
import tempfile
import time
import urllib.request
from typing import List, Dict, Any

import pexels
import renderer
import telegram_api as tg
from config import (
    GITHUB_TOKEN,
    GITHUB_REPO_OWNER,
    GITHUB_REPO_NAME,
    GITHUB_BRANCH,
)

log = logging.getLogger("tools")


# ============================================================
# Tool definitions (Anthropic format)
# ============================================================
TOOL_SCHEMAS = [
    {
        "name": "search_pexels",
        "description": (
            "Cerca foto stock free su Pexels. Restituisce 4-8 immagini con id e URL. "
            "Usa query in INGLESE descrittiva (es. 'marble floor stain', "
            "'wine glass on marble', 'opaque marble surface bright'). "
            "Le immagini saranno mostrate a Claude nella risposta del tool "
            "(come image content blocks) per permettere la scelta visiva."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Query di ricerca in inglese, 2-5 parole.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Numero massimo di risultati (default 6).",
                    "default": 6,
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "apply_image_change",
        "description": (
            "Sostituisce la FOTO di un post pending, re-renderizza il PNG, lo committa "
            "su GitHub e invia una nuova preview Telegram con i bottoni di approvazione. "
            "Il testo e la caption restano invariati. Usa overlay_alpha basso (0.15-0.25) "
            "per foto già chiare, alto (0.45-0.60) per foto scure."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "post_id": {
                    "type": "string",
                    "description": (
                        "L'ID del post da modificare (es. 'daily_02_macchie_vino'). "
                        "Trova quello pending nel contesto della conversazione."
                    ),
                },
                "image_url": {
                    "type": "string",
                    "description": "URL Pexels diretto della nuova foto (da search_pexels).",
                },
                "overlay_alpha": {
                    "type": "number",
                    "description": "Opacità overlay scuro 0.0-1.0. Default 0.30.",
                    "default": 0.30,
                },
                "backdrop_intensity": {
                    "type": "integer",
                    "description": "Intensità backdrop morbido dietro al testo 0-255. Default 130.",
                    "default": 130,
                },
            },
            "required": ["post_id", "image_url"],
        },
    },
    {
        "name": "apply_text_change",
        "description": (
            "Modifica le DUE righe di testo del post (riga 1 bianca, riga 2 lime). "
            "Re-renderizza con la foto attuale, committa, manda nuova preview Telegram."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "post_id": {"type": "string"},
                "line1": {
                    "type": "string",
                    "description": "Prima riga (rendered in bianco). Max 30 caratteri.",
                },
                "line2": {
                    "type": "string",
                    "description": "Seconda riga (rendered in lime). Max 30 caratteri.",
                },
            },
            "required": ["post_id", "line1", "line2"],
        },
    },
    {
        "name": "apply_caption_change",
        "description": (
            "Aggiorna SOLO la caption del post (non re-renderizza l'immagine). "
            "Usalo quando l'utente vuole cambiare il testo Instagram ma non l'immagine."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "post_id": {"type": "string"},
                "new_caption": {
                    "type": "string",
                    "description": (
                        "Caption Instagram completa, con emoji + hashtag. "
                        "Hashtag con # diretto (mai %23). Stile CamelCase brand."
                    ),
                },
            },
            "required": ["post_id", "new_caption"],
        },
    },
    {
        "name": "reply_to_user",
        "description": (
            "Manda un messaggio testuale a Nino su Telegram. "
            "Usalo per spiegare cosa stai facendo, fare domande, o per il messaggio "
            "finale dopo aver completato le modifiche. Tono italiano, conciso, emoji moderati."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "Testo da inviare. HTML supportato (<b>, <i>, <code>).",
                },
            },
            "required": ["text"],
        },
    },
]


# ============================================================
# Helper: GitHub Contents API write
# ============================================================
def _github_write(path: str, content_bytes: bytes, message: str) -> bool:
    """PUT a file to the repo via Contents API. Handles update (sha) + create."""
    base = (
        f"https://api.github.com/repos/"
        f"{GITHUB_REPO_OWNER}/{GITHUB_REPO_NAME}/contents/{path}"
    )
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
    }

    # Get current sha if file exists (required for update)
    sha = None
    try:
        get_req = urllib.request.Request(
            f"{base}?ref={GITHUB_BRANCH}", headers=headers
        )
        with urllib.request.urlopen(get_req, timeout=15) as r:
            meta = json.loads(r.read())
            sha = meta.get("sha")
    except Exception:
        pass  # file doesn't exist yet — that's fine, we'll create it

    payload = {
        "message": message,
        "content": base64.b64encode(content_bytes).decode("ascii"),
        "branch": GITHUB_BRANCH,
    }
    if sha:
        payload["sha"] = sha

    try:
        put_req = urllib.request.Request(
            base, data=json.dumps(payload).encode("utf-8"),
            method="PUT", headers=headers,
        )
        with urllib.request.urlopen(put_req, timeout=20) as r:
            r.read()
        log.info(f"github write OK: {path}")
        return True
    except Exception as e:
        log.exception(f"github write {path}: {e}")
        return False


def _github_read(path: str) -> bytes:
    """GET file content via Contents API (works for private repos)."""
    url = (
        f"https://api.github.com/repos/{GITHUB_REPO_OWNER}/{GITHUB_REPO_NAME}/"
        f"contents/{path}?ref={GITHUB_BRANCH}"
    )
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    with urllib.request.urlopen(req, timeout=15) as r:
        meta = json.loads(r.read())
    if meta.get("encoding") == "base64":
        return base64.b64decode(meta["content"])
    raise RuntimeError(f"Unexpected encoding for {path}")


def _load_post(post_id: str) -> Dict[str, Any]:
    """Load posts/<post_id>.json from repo."""
    safe = post_id.replace("/", "_").replace("..", "_")
    raw = _github_read(f"posts/{safe}.json")
    return json.loads(raw)


# ============================================================
# Tool: search_pexels
# ============================================================
def search_pexels(query: str, limit: int = 6) -> Dict[str, Any]:
    results = pexels.search(query, limit=limit)
    if not results:
        return {
            "results": [],
            "note": f"No results for query={query!r}. Try a different phrasing.",
        }

    # Download each as base64 so Claude can SEE them in the next message
    images_b64: List[Dict] = []
    for r in results:
        try:
            with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
                tmp = f.name
            ok = pexels.download(r["url"], tmp, width=600)
            if ok:
                with open(tmp, "rb") as fh:
                    b64 = base64.standard_b64encode(fh.read()).decode("ascii")
                images_b64.append({
                    "id": r["id"], "url": r["url"], "b64": b64,
                })
            try:
                os.unlink(tmp)
            except OSError:
                pass
        except Exception as e:
            log.warning(f"pexels download {r['id']}: {e}")

    return {
        "results": [{"id": x["id"], "url": x["url"]} for x in images_b64],
        "_images_b64": images_b64,  # consumed by agent.py to build image blocks
    }


# ============================================================
# Tool: apply_image_change
# ============================================================
def apply_image_change(
    post_id: str,
    image_url: str,
    overlay_alpha: float = 0.30,
    backdrop_intensity: int = 130,
) -> Dict[str, Any]:
    # 1. Load post metadata
    try:
        post = _load_post(post_id)
    except Exception as e:
        return {"ok": False, "error": f"post {post_id!r} not found: {e}"}

    line1 = post.get("hook_line1") or post.get("line1") or ""
    line2 = post.get("hook_line2") or post.get("line2") or ""
    if not line1 or not line2:
        # Best-effort: try to extract from caption first line (sometimes stored that way)
        return {
            "ok": False,
            "error": (
                "post metadata manca dei campi hook_line1/hook_line2. "
                "Aggiungerli o usare apply_text_change prima."
            ),
        }

    # 2. Download new photo to temp
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
        photo_tmp = f.name
    ok = pexels.download(image_url, photo_tmp, width=1500)
    if not ok:
        return {"ok": False, "error": f"download fallito: {image_url}"}

    # 3. Render new PNG
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        out_tmp = f.name
    try:
        renderer.render(
            photo_path=photo_tmp,
            line1=line1,
            line2=line2,
            output_path=out_tmp,
            overlay_alpha=overlay_alpha,
            backdrop_intensity=backdrop_intensity,
        )
    except Exception as e:
        return {"ok": False, "error": f"render fallito: {e}"}

    # 4. Commit PNG to repo
    safe = post_id.replace("/", "_").replace("..", "_")
    with open(out_tmp, "rb") as fh:
        png_bytes = fh.read()
    if not _github_write(
        f"posts/{safe}.png", png_bytes,
        f"post: {safe} — image updated by agent",
    ):
        return {"ok": False, "error": "github commit fallito"}

    # 5. Send new Telegram preview
    raw_url = (
        f"https://raw.githubusercontent.com/"
        f"{GITHUB_REPO_OWNER}/{GITHUB_REPO_NAME}/{GITHUB_BRANCH}/"
        f"posts/{safe}.png?v={int(time.time())}"
    )
    caption = post.get("caption", "")

    tg.call("sendPhoto", {
        "chat_id": int(os.environ.get("TELEGRAM_CHAT_ID", "0")),
        "photo": raw_url,
        "caption": f"🖼 Anteprima aggiornata · post_id: <code>{safe}</code>",
        "parse_mode": "HTML",
    })

    tg.call("sendMessage", {
        "chat_id": int(os.environ.get("TELEGRAM_CHAT_ID", "0")),
        "text": "✏️ <b>Caption proposta:</b>\n\n" + (caption[:3500] if caption else ""),
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "reply_markup": {
            "inline_keyboard": [
                [{"text": "✅ Approva e pubblica", "callback_data": f"approve_{safe}"}],
                [
                    {"text": "✏️ Chiedi modifiche", "callback_data": f"modify_{safe}"},
                    {"text": "❌ Scarta", "callback_data": f"discard_{safe}"},
                ],
            ]
        },
    })

    # Cleanup
    for p in (photo_tmp, out_tmp):
        try:
            os.unlink(p)
        except OSError:
            pass

    return {
        "ok": True,
        "post_id": safe,
        "image_url": image_url,
        "raw_url": raw_url,
        "note": "Nuova preview inviata a Telegram con bottoni di approvazione.",
    }


# ============================================================
# Tool: apply_text_change
# ============================================================
def apply_text_change(post_id: str, line1: str, line2: str) -> Dict[str, Any]:
    try:
        post = _load_post(post_id)
    except Exception as e:
        return {"ok": False, "error": f"post {post_id!r} not found: {e}"}

    safe = post_id.replace("/", "_").replace("..", "_")

    # Need the current image — download from raw URL
    image_url_raw = post.get("image_url") or (
        f"https://raw.githubusercontent.com/"
        f"{GITHUB_REPO_OWNER}/{GITHUB_REPO_NAME}/{GITHUB_BRANCH}/"
        f"posts/{safe}.png"
    )
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        photo_tmp = f.name
    try:
        req = urllib.request.Request(
            image_url_raw,
            headers={
                "Authorization": f"Bearer {GITHUB_TOKEN}",
                "User-Agent": "russo-webhook",
            } if "raw.githubusercontent.com" not in image_url_raw else {
                "User-Agent": "russo-webhook",
            },
        )
        # raw.githubusercontent doesn't need auth for public repo
        with urllib.request.urlopen(req, timeout=20) as r:
            with open(photo_tmp, "wb") as fh:
                fh.write(r.read())
    except Exception as e:
        return {"ok": False, "error": f"can't fetch current image: {e}"}

    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        out_tmp = f.name
    try:
        renderer.render(
            photo_path=photo_tmp, line1=line1, line2=line2,
            output_path=out_tmp,
            # we lose original alpha settings here; use moderate defaults
            overlay_alpha=0.30, backdrop_intensity=130,
        )
    except Exception as e:
        return {"ok": False, "error": f"render fallito: {e}"}

    with open(out_tmp, "rb") as fh:
        png_bytes = fh.read()
    if not _github_write(
        f"posts/{safe}.png", png_bytes,
        f"post: {safe} — text updated by agent",
    ):
        return {"ok": False, "error": "github commit fallito"}

    # Update JSON metadata too (record new text)
    post["hook_line1"] = line1
    post["hook_line2"] = line2
    meta_bytes = (
        json.dumps(post, indent=2, ensure_ascii=False) + "\n"
    ).encode("utf-8")
    _github_write(
        f"posts/{safe}.json", meta_bytes,
        f"post: {safe} — text metadata updated",
    )

    raw_url = (
        f"https://raw.githubusercontent.com/"
        f"{GITHUB_REPO_OWNER}/{GITHUB_REPO_NAME}/{GITHUB_BRANCH}/"
        f"posts/{safe}.png?v={int(time.time())}"
    )

    tg.call("sendPhoto", {
        "chat_id": int(os.environ.get("TELEGRAM_CHAT_ID", "0")),
        "photo": raw_url,
        "caption": f"🖼 Anteprima aggiornata (testo) · post_id: <code>{safe}</code>",
        "parse_mode": "HTML",
    })

    tg.call("sendMessage", {
        "chat_id": int(os.environ.get("TELEGRAM_CHAT_ID", "0")),
        "text": (
            "✏️ <b>Caption proposta:</b>\n\n"
            + (post.get("caption", "")[:3500] or "")
        ),
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "reply_markup": {
            "inline_keyboard": [
                [{"text": "✅ Approva e pubblica", "callback_data": f"approve_{safe}"}],
                [
                    {"text": "✏️ Chiedi modifiche", "callback_data": f"modify_{safe}"},
                    {"text": "❌ Scarta", "callback_data": f"discard_{safe}"},
                ],
            ]
        },
    })

    for p in (photo_tmp, out_tmp):
        try:
            os.unlink(p)
        except OSError:
            pass

    return {"ok": True, "post_id": safe, "raw_url": raw_url}


# ============================================================
# Tool: apply_caption_change
# ============================================================
def apply_caption_change(post_id: str, new_caption: str) -> Dict[str, Any]:
    try:
        post = _load_post(post_id)
    except Exception as e:
        return {"ok": False, "error": f"post {post_id!r} not found: {e}"}

    safe = post_id.replace("/", "_").replace("..", "_")
    post["caption"] = new_caption
    meta_bytes = (
        json.dumps(post, indent=2, ensure_ascii=False) + "\n"
    ).encode("utf-8")
    if not _github_write(
        f"posts/{safe}.json", meta_bytes,
        f"post: {safe} — caption updated by agent",
    ):
        return {"ok": False, "error": "github commit fallito"}

    tg.call("sendMessage", {
        "chat_id": int(os.environ.get("TELEGRAM_CHAT_ID", "0")),
        "text": (
            f"✏️ <b>Caption aggiornata</b> per <code>{safe}</code>:\n\n"
            + new_caption[:3500]
        ),
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "reply_markup": {
            "inline_keyboard": [
                [{"text": "✅ Approva e pubblica", "callback_data": f"approve_{safe}"}],
                [
                    {"text": "✏️ Chiedi altre modifiche", "callback_data": f"modify_{safe}"},
                    {"text": "❌ Scarta", "callback_data": f"discard_{safe}"},
                ],
            ]
        },
    })
    return {"ok": True, "post_id": safe}


# ============================================================
# Tool: reply_to_user
# ============================================================
def reply_to_user(text: str) -> Dict[str, Any]:
    r = tg.call("sendMessage", {
        "chat_id": int(os.environ.get("TELEGRAM_CHAT_ID", "0")),
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    })
    return {"ok": bool(r.get("ok"))}


# ============================================================
# Tool dispatch
# ============================================================
TOOL_HANDLERS = {
    "search_pexels": search_pexels,
    "apply_image_change": apply_image_change,
    "apply_text_change": apply_text_change,
    "apply_caption_change": apply_caption_change,
    "reply_to_user": reply_to_user,
}
