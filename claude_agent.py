"""
Claude agent loop — drives autonomous modifications via Anthropic API + tools.

Input: a Telegram text message from the user (e.g., "voglio una foto più chiara").
Output: actions executed (image change, text change, etc.) + replies on Telegram.

Loop:
  1. Build system prompt + initial user message with context
  2. Call Anthropic API
  3. If response has tool_use blocks: execute tools, append results, loop
  4. If response has only text: send to Telegram and stop
"""
import base64
import json
import logging
import os
from typing import List, Dict, Any

from config import (
    ANTHROPIC_API_KEY,
    ANTHROPIC_MODEL,
    GITHUB_TOKEN,
    GITHUB_REPO_OWNER,
    GITHUB_REPO_NAME,
    GITHUB_BRANCH,
)
import tools
import telegram_api as tg

log = logging.getLogger("agent")


SYSTEM_PROMPT = """Sei l'assistente AI di Russo Pavimenti, gestisci il loro Instagram (@pavimenti_russo).

Brand:
- Lucidatura/levigatura marmo, marmocemento, top cucina/bagno, scale, rivestimenti
- Sicilia occidentale (Alcamo + provincia Trapani + Palermo)
- Telefono: +39 339 7919513 — sito: russopavimenti.com

Stile post (importante):
- Foto stock free da Pexels — chiare, dettagli reali, mood magazine
- 1 sola frase d'impatto centrata verticalmente
- Riga 1 in bianco, riga 2 in lime (#D8FF3D)
- Font Old Standard TT (mai italic)
- Monogramma "MR" piccolo in basso

Voice caption Instagram:
- Italiano poetico ma diretto
- Emoji moderati: ✨🤔💡✅❤️🍷📞🌐
- Hashtag CamelCase brand (#LucidaturaPavimenti #RestauroMarmo ecc.) + città lowercase
- # diretto, MAI %23

Stai parlando con Nino (founder) via Telegram. Lui ti scrive richieste di modifica ai post pendenti.

Hai questi tool:
- search_pexels: cerca foto stock. Query in INGLESE (es. "marble floor stain", "wine on stone"). Le immagini ti vengono mostrate inline per scegliere visivamente.
- apply_image_change: cambia la FOTO del post + re-renderizza + manda nuova preview Telegram con bottoni approvazione
- apply_text_change: cambia le 2 righe di testo del post
- apply_caption_change: cambia la caption Instagram (no re-render)
- reply_to_user: manda un messaggio testuale (per spiegazioni, ack, domande)

PROCEDURA OBBLIGATORIA — segui esattamente questi passaggi, in ordine:

PASSO 1: chiamata UNICA a reply_to_user con un breve ack ("Cerco foto X 🔍")

PASSO 2: chiamata UNICA a search_pexels con una query ben pensata. NON fare query multiple — se i risultati non ti convincono, scegli comunque il migliore tra quelli mostrati.

PASSO 3: chiamata UNICA al tool apply_*_change appropriato.
   - apply_image_change per cambi foto
   - apply_text_change per cambi testo nelle 2 righe sul post
   - apply_caption_change per cambi caption Instagram

PASSO 4: chiamata UNICA finale a reply_to_user per concludere (es. "Pronta v2, controlla la preview qui sopra ✅")

NON chiamare lo stesso tool due volte. NON cercare foto multiple volte. NON cercare conferma — agisci.
NON dire mai "Claude leggerà al prossimo accesso" — sei TU Claude, stai rispondendo ORA.

Massimo 4 chiamate di tool totali. Se non trovi quello che cerchi al primo search, scegli comunque.
"""


def _load_recent_post() -> Dict[str, Any]:
    """Load the most recently modified posts/*.json from the repo."""
    import urllib.request
    url = (
        f"https://api.github.com/repos/{GITHUB_REPO_OWNER}/"
        f"{GITHUB_REPO_NAME}/contents/posts?ref={GITHUB_BRANCH}"
    )
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as r:
            items = json.loads(r.read())
    except Exception as e:
        log.warning(f"list posts failed: {e}")
        return {}

    json_files = [
        x for x in items
        if x.get("name", "").endswith(".json") and x.get("type") == "file"
    ]
    if not json_files:
        return {}

    # No good 'last modified' field via Contents API; pick last alphabetically
    # (post_ids are usually daily_NN_*; bigger NN = more recent)
    latest = sorted(json_files, key=lambda x: x["name"])[-1]
    try:
        req = urllib.request.Request(
            latest["url"], headers=headers,
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            meta = json.loads(r.read())
        if meta.get("encoding") == "base64":
            content = base64.b64decode(meta["content"])
            return json.loads(content)
    except Exception as e:
        log.warning(f"load latest post: {e}")
    return {}


def _post_context_block(post: Dict[str, Any]) -> str:
    if not post:
        return "(Nessun post pending trovato.)"
    return (
        f"Post attualmente in attesa di approvazione:\n"
        f"- post_id: {post.get('post_id', '?')}\n"
        f"- topic: {post.get('topic', '?')}\n"
        f"- hook line 1: {post.get('hook_line1') or '(estratto dalla caption)'}\n"
        f"- hook line 2: {post.get('hook_line2') or '(estratto dalla caption)'}\n"
        f"- caption (primi 400 char): {(post.get('caption') or '')[:400]}..."
    )


def _build_image_blocks(b64_list: List[Dict]) -> List[Dict]:
    """Build content blocks with images for Claude to see."""
    out = []
    for img in b64_list:
        out.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": img["b64"],
            },
        })
        out.append({
            "type": "text",
            "text": f"↑ id={img['id']} url={img['url']}",
        })
    return out


def run(user_text: str, user_name: str = "Nino") -> None:
    """Run the agent loop for a single user message."""
    if not ANTHROPIC_API_KEY:
        log.warning("ANTHROPIC_API_KEY missing — falling back to plain ACK")
        tg.send_message(
            "📥 Richiesta ricevuta — Claude non è attivo, l'assistente "
            "Mac la processerà appena disponibile.",
        )
        return

    try:
        from anthropic import Anthropic
    except ImportError:
        log.error("anthropic package not installed")
        return

    client = Anthropic(api_key=ANTHROPIC_API_KEY)

    # Load post context
    post = _load_recent_post()
    context = _post_context_block(post)

    initial_user_msg = (
        f"Richiesta di {user_name}:\n\n«{user_text}»\n\n---\n{context}"
    )

    messages: List[Dict[str, Any]] = [
        {"role": "user", "content": initial_user_msg}
    ]

    MAX_TURNS = 15
    for turn in range(MAX_TURNS):  # safety cap on agent iterations
        log.info(f"agent turn {turn + 1}")
        print(f"[agent] turn {turn + 1}/{MAX_TURNS}", flush=True)
        try:
            resp = client.messages.create(
                model=ANTHROPIC_MODEL,
                max_tokens=2000,
                system=SYSTEM_PROMPT,
                tools=tools.TOOL_SCHEMAS,
                messages=messages,
            )
        except Exception as e:
            log.exception("anthropic api call failed")
            tg.send_message(
                f"⚠️ Errore dell'AI: <code>{str(e)[:300]}</code>\n\n"
                "Riprova o scrivi a Claude su Mac."
            )
            return

        # Build assistant message (verbatim) for next turn
        assistant_blocks = []
        tool_uses = []
        text_to_user = []
        for block in resp.content:
            if block.type == "text" and block.text:
                text_to_user.append(block.text)
                assistant_blocks.append({"type": "text", "text": block.text})
            elif block.type == "tool_use":
                tool_uses.append(block)
                assistant_blocks.append({
                    "type": "tool_use",
                    "id": block.id,
                    "name": block.name,
                    "input": block.input,
                })

        messages.append({"role": "assistant", "content": assistant_blocks})

        # Don't auto-send raw text mid-loop unless it's the final turn
        # (text mid-loop is usually thinking-out-loud)
        if resp.stop_reason == "end_turn" and text_to_user and not tool_uses:
            # Final reply — already sent via reply_to_user normally, but
            # if model finished with plain text and no tool, send anyway.
            tg.send_message("\n\n".join(text_to_user))
            return

        if not tool_uses:
            # No more tools and no clear final text → stop loop
            if text_to_user:
                tg.send_message("\n\n".join(text_to_user))
            return

        # Execute tools, build user message with tool_result blocks
        tool_result_blocks = []
        for tu in tool_uses:
            tname = tu.name
            tinput = tu.input or {}
            log.info(f"tool call: {tname}({json.dumps(tinput)[:200]})")
            print(f"[agent] tool: {tname}({json.dumps(tinput)[:200]})", flush=True)
            handler = tools.TOOL_HANDLERS.get(tname)
            if not handler:
                tool_result_blocks.append({
                    "type": "tool_result",
                    "tool_use_id": tu.id,
                    "content": json.dumps({"error": f"unknown tool {tname}"}),
                    "is_error": True,
                })
                continue
            try:
                result = handler(**tinput)
                print(f"[agent] tool {tname} → ok: {str(result)[:200]}", flush=True)
            except Exception as e:
                log.exception(f"tool {tname} crashed")
                print(f"[agent] tool {tname} CRASHED: {e}", flush=True)
                tool_result_blocks.append({
                    "type": "tool_result",
                    "tool_use_id": tu.id,
                    "content": json.dumps({"error": str(e)}),
                    "is_error": True,
                })
                continue

            # Special: for search_pexels, attach the images so Claude can see
            content_blocks: Any
            if tname == "search_pexels" and isinstance(result, dict):
                img_list = result.pop("_images_b64", [])
                # tool_result content must be string or list of blocks
                content_blocks = [
                    {"type": "text", "text": json.dumps(result, ensure_ascii=False)}
                ]
                if img_list:
                    content_blocks.extend(_build_image_blocks(img_list))
            else:
                content_blocks = json.dumps(result, ensure_ascii=False)

            tool_result_blocks.append({
                "type": "tool_result",
                "tool_use_id": tu.id,
                "content": content_blocks,
            })

        messages.append({"role": "user", "content": tool_result_blocks})
        # loop continues

    log.warning("agent loop hit max turns")
    tg.send_message(
        "⚠️ L'AI ha esaurito i passi disponibili. "
        "Controlla cosa è cambiato sul bot — può essere già fatto."
    )
