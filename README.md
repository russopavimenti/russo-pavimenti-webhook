# Russo Pavimenti — Telegram Webhook

Webhook server che riceve i click sui pulsanti del bot Telegram
`@Russo_Pavimenti_ai_bot` e pubblica i post approvati su Instagram
`@pavimenti_russo` via Composio.

Architettura: webhook **always-on su Render** (free tier) → quando arriva
un click di approvazione su Telegram, viene processato istantaneamente
indipendentemente dallo stato del Mac dell'utente.

```
┌─────────────────────┐
│  Claude su Mac      │  1. Renderizza post (PNG + caption)
│                     │  2. Commit + push su GitHub repo (questo)
└──────────┬──────────┘  3. Invia preview su Telegram con bottoni
           │
           ▼ (Telegram preview viewable on phone)
┌─────────────────────┐
│  Utente Telegram    │  4. Clicca ✅ Approva
└──────────┬──────────┘
           │
           ▼
┌─────────────────────┐
│  Telegram Bot API   │  5. Invia callback_query a webhook URL
└──────────┬──────────┘
           │
           ▼
┌─────────────────────┐
│  Render webhook     │  6. Legge metadata da GitHub
│  (questo repo)      │  7. Pubblica su IG via Composio
│                     │  8. Risponde a Telegram con permalink
└─────────────────────┘
```

## Setup deployment (one-time, ~10 minuti)

### 1. Variabili d'ambiente che servono

Genera 2 random secret:

```bash
openssl rand -hex 16   # → useralo come WEBHOOK_PATH_SECRET
openssl rand -hex 16   # → useralo come TELEGRAM_WEBHOOK_SECRET
```

Procurati la **Composio API key**:
[app.composio.dev/developers](https://app.composio.dev/developers) → "Create new key"

### 2. Deploy su Render

1. Vai su [render.com](https://render.com) → **New +** → **Blueprint**
2. Connetti questo repo (`russo-pavimenti-webhook`)
3. Render legge automaticamente `render.yaml` e crea il servizio
4. Quando ti chiede le variabili `sync: false`, inserisci:
   - `TELEGRAM_BOT_TOKEN`: il token del bot (da @BotFather)
   - `TELEGRAM_CHAT_ID`: il tuo chat_id (intero)
   - `COMPOSIO_API_KEY`: la chiave da app.composio.dev
   - `TELEGRAM_WEBHOOK_SECRET`: il primo random hex sopra
   - `WEBHOOK_PATH_SECRET`: il secondo random hex sopra
   - `GITHUB_TOKEN`: SOLO se il repo è privato (PAT con scope `repo:contents:read`)
5. Click **Apply / Deploy**
6. Aspetta che il deploy finisca (~2-3 min). Quando è "Live", copia l'URL pubblico, es:
   ```
   https://russo-pavimenti-webhook.onrender.com
   ```

### 3. Configura il webhook Telegram

Sostituisci `<URL>`, `<PATH_SECRET>`, `<HEADER_SECRET>`, `<BOT_TOKEN>`
con i tuoi valori:

```bash
curl -X POST "https://api.telegram.org/bot<BOT_TOKEN>/setWebhook" \
  -H "Content-Type: application/json" \
  -d '{
    "url": "<URL>/webhook/<PATH_SECRET>",
    "secret_token": "<HEADER_SECRET>",
    "allowed_updates": ["callback_query"]
  }'
```

Verifica:
```bash
curl "https://api.telegram.org/bot<BOT_TOKEN>/getWebhookInfo"
```
Deve mostrare il tuo URL e `pending_update_count: 0`.

### 4. Test

1. Apri Telegram e clicca uno dei bottoni di approvazione su un post precedente
2. Vai su Render dashboard → **Logs** → dovresti vedere `update: keys=['update_id', 'callback_query']`
3. Su Telegram dovresti ricevere il messaggio di conferma con il permalink IG

## Come Claude (Mac side) registra nuovi post

Per ogni nuovo post:
1. Renderizza il PNG (script Python esistente)
2. Crea `posts/<post_id>.json` con questo schema:
   ```json
   {
     "post_id": "daily_02_macchie_vino",
     "image_url": "https://raw.githubusercontent.com/russopavimenti/russo-pavimenti-webhook/main/posts/daily_02_macchie_vino.png",
     "caption": "<full IG caption with hashtags>",
     "topic": "Macchie di vino sul marmo",
     "created_at": "2026-05-16T22:00:00Z"
   }
   ```
3. Copia il PNG in `posts/<post_id>.png`
4. `git add posts/ && git commit -m "post: <post_id>" && git push`
5. Invia preview Telegram con `callback_data=approve_<post_id>`

## Sicurezza

- ✅ Tutti i secret sono in env vars Render, mai nel repo
- ✅ URL webhook contiene path segreto (`/webhook/<random>`)
- ✅ Verifica `X-Telegram-Bot-Api-Secret-Token` header → rigetta tutto il resto con 403
- ✅ Repo .gitignore esclude `secrets.env` per uso locale
- ⚠️ `image_url` e `caption` nei file `posts/*.json` saranno pubblici se il
  repo è pubblico — sono comunque destinati a IG (già pubblici per definizione)

## File principali

```
.
├── app.py              # Flask webhook + handlers (approve/modify/discard)
├── publisher.py        # Composio SDK → Instagram publish (2-step)
├── github_storage.py   # Read post metadata dal repo (raw + API+PAT)
├── telegram_api.py     # Wrapper Telegram Bot API (sendMessage, edit, ACK)
├── config.py           # Env-driven config (NO secrets hardcoded)
├── render.yaml         # Render Blueprint
├── requirements.txt    # Dipendenze pinned
├── .env.example        # Template variabili (copy → secrets.env per dev)
└── posts/              # Metadata + immagini dei post in attesa di approvazione
```
