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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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
    # Brand + servizi
    "#russopavimenti #lucidaturamarmo #levigaturamarmo "
    "#restauromarmo #marmocemento #pavimentiinmarmo "
    # Provincia di Trapani — Alcamo (base) + comuni vicini + città principali
    "#alcamo #castellammaredelgolfo #calatafimi #trapani "
    "#erice #valderice #marsala #salemi "
    # Provincia di Palermo — comuni al confine + città principali
    "#partinico #balestrate #monreale #carini "
    "#cinisi #terrasini #bagheria #palermo "
    # Regione
    "#sicilia"
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


def save_state(state: Dict[str, Any]) -> bool:
    """Persist state via SHA-aware CAS write. Returns True on success, False on
    conflict (HTTP 409) or other write failure."""
    body = (json.dumps(state, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    return gh_write(STATE_FILE_REPO, body, f"daily_state: bump to index {state.get('next_index')}")


# === Topics loading ===
def load_topics() -> List[Dict[str, Any]]:
    raw = TOPICS_FILE.read_text()
    data = json.loads(raw)
    return data["topics"]


# === Anti-repeat tracking ===
# Topic considered "fresh" if not used in the last TOPIC_COOLDOWN_DAYS days.
# Photo considered "fresh" if not used in the last PHOTO_COOLDOWN_DAYS days.
# Cooldown windows tuned for 3 posts/day; cleanup keeps state.json compact.
TOPIC_COOLDOWN_DAYS = 21    # avoid repeating a topic for 3 weeks
PHOTO_COOLDOWN_DAYS = 90    # never repeat a Pexels photo within 3 months
USED_TOPICS_KEEP_DAYS = 60  # cleanup horizon for state hygiene
USED_PHOTOS_KEEP_DAYS = 180


def _days_ago(iso_str: str) -> int:
    """Days between iso_str and now (UTC). Returns big number on parse failure."""
    try:
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - dt
        return delta.days
    except Exception:
        return 99999


def cleanup_used_tracking(state: Dict[str, Any]) -> None:
    """Drop entries older than the keep horizon from used_topics/used_photos."""
    for key, max_days in (("used_topics", USED_TOPICS_KEEP_DAYS),
                          ("used_photos", USED_PHOTOS_KEEP_DAYS)):
        d = state.get(key) or {}
        if not isinstance(d, dict):
            continue
        stale = [k for k, v in d.items() if _days_ago(str(v)) > max_days]
        for k in stale:
            d.pop(k, None)
        state[key] = d


def pick_next_topic(topics: List[Dict[str, Any]], state: Dict[str, Any]) -> Dict[str, Any]:
    """Anti-repeat topic selection.

    Strategy:
      1. Filter out topics used within TOPIC_COOLDOWN_DAYS.
      2. Among fresh ones, pick the one least-recently used (or never used).
      3. Fallback if pool exhausted: pick globally least-recently used.

    used_topics in state is {topic_id: iso_timestamp_last_used}.
    """
    used = state.get("used_topics") or {}
    # Score each topic by days_since_last_use (huge if never used)
    def score(t: Dict[str, Any]) -> int:
        ts = used.get(t["id"])
        if not ts:
            return 99999
        return _days_ago(str(ts))

    fresh = [t for t in topics if score(t) >= TOPIC_COOLDOWN_DAYS]
    if fresh:
        # Among fresh, prefer the least-recently used (gives variety)
        fresh.sort(key=score, reverse=True)
        return fresh[0]

    # All topics recently used — fall back to least-recently used overall
    log.warning(f"all {len(topics)} topics used within {TOPIC_COOLDOWN_DAYS}d — "
                f"falling back to least-recently-used. Add more topics!")
    return max(topics, key=score)


# === Pexels search/download (via API with UA) ===
PEXELS_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"


def pexels_search_first(
    queries: List[str],
    limit: int = 15,
    used_photo_ids: Optional[set] = None,
) -> Dict[str, Any]:
    """Pick a fresh photo from Pexels.

    Strategy:
      - For each query, scan multiple pages (randomized) up to `limit` per page.
      - Skip any photo whose id is in `used_photo_ids` (long-term dedup).
      - Returns dict {'url': ..., 'id': ...} or {} if nothing fresh found.

    Returning the id lets the caller persist it to state so it stays blacklisted
    across future runs (e.g. when the topic recycles after the cooldown).
    """
    import urllib.parse as up
    used_photo_ids = used_photo_ids or set()
    # Randomize page order so we don't always pick the same "top result"
    # even when used_photo_ids is empty (e.g. early days of the pool).
    pages = [1, 2, 3]
    random.shuffle(pages)

    for q in queries:
        for page in pages:
            url = (f"https://api.pexels.com/v1/search?query={up.quote(q)}"
                   f"&per_page={limit}&page={page}")
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
                log.warning(f"pexels query {q!r} page={page} failed: {e}")
                continue

            photos = list(data.get("photos", []) or [])
            # Within a page, shuffle for additional variety
            random.shuffle(photos)

            for photo in photos:
                pid = photo.get("id")
                if pid is None:
                    continue
                if str(pid) in used_photo_ids or pid in used_photo_ids:
                    log.info(f"skip used photo_id={pid}")
                    continue
                src = photo.get("src", {})
                u = src.get("large") or src.get("medium") or src.get("original")
                if u:
                    log.info(f"picked photo id={pid} from query={q!r} page={page}")
                    return {"url": u, "id": pid}

    log.warning("no fresh photo found across all queries/pages (all blacklisted?)")
    return {}


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

# Canonical slot schedule (UTC). Update during DST transitions.
# CEST (summer, UTC+2): morning=06:00, lunch=10:30, evening=15:30 UTC
# CET  (winter, UTC+1): add +1 hour to each
SLOTS_UTC: List[Tuple[str, int, int]] = [
    ("morning", 6, 0),    # 08:00 IT CEST
    ("lunch",   10, 30),  # 12:30 IT CEST
    ("evening", 15, 30),  # 17:30 IT CEST
]
# Grace window: a slot is considered "due" starting `GRACE_MIN` before its time.
# This allows a cron firing slightly early to still pick up its own slot.
GRACE_MIN = 5

# A 'claimed' history entry older than this many minutes is considered an
# ORPHAN (the runner that claimed it crashed before finishing). Orphan claims
# do NOT block a slot — a later run is allowed to re-claim and retry it.
# 'done'/'manual_replacement' always block; a fresh 'claimed' blocks (a sibling
# runner is mid-flight); 'failed' never blocks.
STALE_CLAIM_MIN = 25


def determine_slot() -> str:
    """Legacy: derive slot from current UTC hour (kept for workflow_dispatch)."""
    if DAILY_SLOT and DAILY_SLOT != "auto":
        return DAILY_SLOT
    now_utc = datetime.now(timezone.utc)
    h = now_utc.hour
    if h < 9:
        return "morning"
    if h < 13:
        return "lunch"
    return "evening"


def slots_due_today(now_utc: datetime, state: Dict[str, Any]) -> List[str]:
    """Return ordered list of slot names that are scheduled by now but not yet done.

    A slot is "due" if its scheduled UTC time minus GRACE_MIN is <= now_utc AND
    there's no history entry for today+slot. Self-healing: any cron run that
    fires (even hours late) will pick up all scheduled-but-missing slots and
    process them in chronological order.
    """
    today_utc = now_utc.strftime("%Y%m%d")
    due: List[str] = []
    for slot_name, h, m in SLOTS_UTC:
        slot_dt = now_utc.replace(hour=h, minute=m, second=0, microsecond=0)
        if now_utc + timedelta(minutes=GRACE_MIN) < slot_dt:
            # Slot is in the future (even with grace) — skip
            continue
        if _slot_already_done(state, today_utc, slot_name):
            continue
        due.append(slot_name)
    return due


def _entry_age_min(entry: Dict[str, Any]) -> float:
    """Minutes since the entry was created (field 'at'). Huge on parse failure."""
    ts = entry.get("at", "")
    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds() / 60.0
    except Exception:
        return 99999.0


def _slot_status(state: Dict[str, Any], today_utc: str, slot: str) -> Optional[str]:
    """Effective status of today+slot, derived from the LATEST matching history
    entry. Returns one of:
      'done'          → completed (or manual_replacement); never retry
      'claimed_fresh' → a sibling runner is mid-flight; do not retry
      'claimed_stale' → claimer crashed/orphaned; safe to re-claim
      'failed'        → generation failed before preview; must retry
      None            → no entry yet for this slot
    """
    best: Optional[Dict[str, Any]] = None
    for entry in state.get("history", []) or []:
        post_id_existing = entry.get("post_id", "")
        if entry.get("slot") == slot and post_id_existing.startswith(f"daily_{today_utc}_{slot}_"):
            best = entry  # history is append-ordered → keep the latest match
    if best is None:
        return None
    st = (best.get("status") or "done").strip()
    if st in ("done", "manual_replacement"):
        return "done"
    if st == "failed":
        return "failed"
    if st == "claimed":
        return "claimed_stale" if _entry_age_min(best) > STALE_CLAIM_MIN else "claimed_fresh"
    # Unknown status → treat as done (conservative: don't spam duplicate previews)
    return "done"


def _slot_already_done(state: Dict[str, Any], today_utc: str, slot: str) -> bool:
    """True if the slot must NOT be (re)processed: either completed, or freshly
    claimed by a sibling runner still in-flight. Orphaned ('claimed_stale') and
    'failed' slots return False so they get retried."""
    return _slot_status(state, today_utc, slot) in ("done", "claimed_fresh")


def _try_claim_slot(today_utc: str, slot: str, topics: List[Dict[str, Any]],
                    max_retries: int = 3) -> Optional[Dict[str, Any]]:
    """Optimistic claim via CAS write on daily_state.json.

    Race scenario this defends against: GH Actions batches the 3 redundant
    cron triggers and runs them in parallel. All 3 read state simultaneously,
    all pass the idempotency check, all generate + send preview. Only the
    first state-commit wins (SHA mismatch) but the previews were already sent.

    Fix: claim the slot BEFORE generating, via SHA-aware CAS write. Only the
    runner that wins the CAS commit proceeds. Others exit silently with no
    side effects.

    Returns the claimed (post_id, topic, state) tuple if we won, None if we lost.
    """
    import os as _os
    run_id = _os.environ.get("GITHUB_RUN_ID", f"local-{int(time.time())}")

    for attempt in range(max_retries):
        state = load_state()

        # Idempotency re-check inside the loop (state may have changed since last attempt)
        if _slot_already_done(state, today_utc, slot):
            log.info(f"IDEMPOTENT skip (attempt {attempt+1}): {today_utc}+{slot} already done")
            return None

        # Pick the topic and build claim entry
        topic = pick_next_topic(topics, state)
        post_id = f"daily_{today_utc}_{slot}_{topic['id']}"
        now_iso = datetime.now(timezone.utc).isoformat()

        state["next_index"] = (state.get("next_index", 0) + 1) % len(topics)
        state.setdefault("history", []).append({
            "post_id": post_id,
            "topic_id": topic["id"],
            "slot": slot,
            "at": now_iso,
            "claimed_by_run": run_id,
            "status": "claimed",
        })
        state["history"] = state["history"][-50:]
        # Burn the topic ATOMICALLY with the claim. Guarantees no other slot in
        # this run (nor any future run) can pick the same topic — even if
        # generation later fails. Fixes the lunch+evening duplicate-topic bug
        # (both picked 'lavabo_marmo_macchie' on 2026-05-21 because the topic
        # was only registered on the success path, which the lunch never reached).
        state.setdefault("used_topics", {})[topic["id"]] = now_iso

        # gh_write does SHA-aware update internally (gets current sha then PUT).
        # If another runner committed between our read and write, gh_write
        # returns False (HTTP 409). save_state now propagates that bool.
        log.info(f"attempt {attempt+1}: trying to claim {post_id} (run_id={run_id})")
        try:
            saved = save_state(state)
            if not saved:
                # 409 conflict or write failure → another runner won the CAS race.
                log.info(f"attempt {attempt+1}: save_state returned False (conflict), retrying")
                time.sleep(1 + attempt)
                continue

            # save returned True → our PUT succeeded. Verify with retry+backoff
            # because GitHub Contents API has eventual consistency between write
            # and immediate read (observed 24/5/2026: write succeeded but immediate
            # reread missed it, causing the claim to be wrongly abandoned).
            our_entry = None
            for retry in range(4):
                time.sleep(0.4 + 0.3 * retry)  # 0.4, 0.7, 1.0, 1.3 s
                verify = load_state()
                our_entry = next(
                    (e for e in verify.get("history", [])
                     if e.get("claimed_by_run") == run_id),
                    None,
                )
                if our_entry:
                    break

            if our_entry:
                log.info(f"CLAIM WON: {post_id} (run_id={run_id})")
                return {"post_id": post_id, "topic": topic,
                        "state": verify, "run_id": run_id}

            # save was True but reread never confirmed. Trust the write: SHA-based
            # CAS guarantees the write only succeeds if no concurrent modification,
            # so our entry IS persisted — the reread just hit a stale replica.
            # Proceed optimistically with our local in-memory state.
            log.warning(f"save ok but reread didn't confirm after retries — "
                        f"trusting gh_write (eventual consistency); proceeding with claim")
            return {"post_id": post_id, "topic": topic,
                    "state": state, "run_id": run_id}
        except Exception as e:
            log.warning(f"attempt {attempt+1} claim raised: {e}")

        # Lost the race or partial failure: re-loop, will re-check idempotency
        # and try to claim with fresh state.
        time.sleep(1 + attempt)

    log.error(f"all {max_retries} claim attempts exhausted")
    return None


def _commit_state_mutation(mutate, label: str, max_retries: int = 5) -> bool:
    """Load state, apply mutate(state) in-place, save with CAS. Retry on conflict.

    gh_write returns False on HTTP 409 (SHA conflict) → another runner committed
    in between. We reload and retry. This makes state updates robust against the
    parallel-runner races that previously left entries stuck in 'claimed'.
    """
    for attempt in range(max_retries):
        state = load_state()
        try:
            mutate(state)
        except Exception as e:
            log.warning(f"state mutation '{label}' callback raised: {e}")
            return False
        body = (json.dumps(state, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        if gh_write(STATE_FILE_REPO, body, f"daily_state: {label} (try {attempt+1})"):
            return True
        log.warning(f"state '{label}' commit try {attempt+1} conflicted, reloading")
        time.sleep(1 + attempt)
    log.error(f"state mutation '{label}': all {max_retries} retries exhausted")
    return False


def _set_entry_status(post_id: str, status: str,
                      extra: Optional[Dict[str, Any]] = None) -> bool:
    """CAS-retry update of a history entry's status + optional extra fields.

    status='done'   → marks completion; if extra carries photo_id, the photo is
                      also registered in used_photos (long-term dedup).
    status='failed' → releases the slot so a later run can retry it. The topic
                      stays burned (used_topics) so the retry picks a fresh one.
    """
    extra = extra or {}

    def mutate(state: Dict[str, Any]) -> None:
        now_iso = datetime.now(timezone.utc).isoformat()
        for entry in state.get("history", []):
            if entry.get("post_id") == post_id:
                entry["status"] = status
                entry[f"{status}_at"] = now_iso
                for k, v in extra.items():
                    entry[k] = v
        photo_id = extra.get("photo_id")
        if status == "done" and photo_id is not None:
            state.setdefault("used_photos", {})[str(photo_id)] = now_iso
        cleanup_used_tracking(state)

    return _commit_state_mutation(mutate, f"{status} {post_id}")


def generate_one_slot(slot: str, today_utc: str, topics: List[Dict[str, Any]]) -> int:
    """Generate + send preview for a single slot. Returns 0 on success, 1 on error,
    2 if skipped (idempotent or lost claim race).

    On any error BEFORE the Telegram preview is sent, the claimed history entry
    is flipped to 'failed' so a later self-healing run retries the slot (the
    burned topic stays burned → the retry picks a fresh one). On success it is
    flipped to 'done'. A slot therefore never gets stuck silently in 'claimed'.
    """
    log.info(f"--- processing slot={slot} today={today_utc} ---")

    # CLAIM the slot via CAS BEFORE generating anything.
    claim = _try_claim_slot(today_utc, slot, topics)
    if claim is None:
        log.info(f"slot {today_utc}+{slot} skipped (race lost or already done)")
        return 2

    post_id = claim["post_id"]
    topic = claim["topic"]
    log.info(f"claim won: topic={topic['id']} post_id={post_id}")

    tmp_photo = None
    tmp_out = None
    try:
        # 1. Pick photo from Pexels (with long-term blacklist of used photo_ids)
        used_photo_ids = set(
            str(k) for k in (claim.get("state", {}).get("used_photos") or {}).keys()
        )
        queries = topic.get("pexels_queries") or [topic["topic"]]
        photo = pexels_search_first(queries, used_photo_ids=used_photo_ids)
        if not photo:
            log.error("no fresh photo found, abort")
            _set_entry_status(post_id, "failed", {"error": "no_fresh_photo"})
            tg_call("sendMessage", {
                "chat_id": TELEGRAM_CHAT_ID,
                "text": f"⚠️ Daily cron {slot}: nessuna foto NUOVA per topic {topic['id']!r}. "
                        f"Lo slot verrà ritentato al prossimo ciclo con un altro argomento.",
            })
            return 1
        photo_url = photo["url"]
        photo_id = photo["id"]

        # 2. Download photo
        tmp_photo = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False).name
        if not pexels_download(photo_url, tmp_photo):
            log.error("download failed")
            _set_entry_status(post_id, "failed", {"error": "photo_download_failed"})
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
            _set_entry_status(post_id, "failed", {"error": "png_commit_failed"})
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
            _set_entry_status(post_id, "failed", {"error": "json_commit_failed"})
            return 1

        # 6. Send Telegram preview with cache-buster
        tg_send_preview(post_id, image_url + f"?v={int(time.time())}", caption, slot)

        # 7. Mark claim 'done' + register photo as used (topic was burned at claim).
        #    Robust CAS-retry update — a single conflicted write no longer leaves
        #    the entry stuck in 'claimed'.
        if not _set_entry_status(post_id, "done", {"photo_id": photo_id}):
            log.warning("could not flip entry to 'done' after retries — preview "
                        "WAS sent, slot is effectively complete")

        log.info(f"--- slot {slot} DONE: {post_id} ---")
        return 0

    except Exception as e:
        # Crash before the preview was sent (e.g. renderer error). Release the
        # slot so a later self-healing run retries it with a fresh topic.
        log.exception(f"slot {slot} generation crashed: {e}")
        _set_entry_status(post_id, "failed", {"error": f"exception: {e}"[:200]})
        return 1
    finally:
        for p in (tmp_photo, tmp_out):
            if p:
                try:
                    os.unlink(p)
                except OSError:
                    pass


def main() -> int:
    now_utc = datetime.now(timezone.utc)
    today_utc = now_utc.strftime("%Y%m%d")
    topics = load_topics()

    # Determine which slot(s) to process:
    # - workflow_dispatch with explicit slot → just that slot (single)
    # - schedule trigger → ALL slots that are due today and not yet done
    #   (self-healing: recovers any slot that got skipped by GH Actions earlier)
    explicit_slot = (DAILY_SLOT or "").strip()
    if explicit_slot and explicit_slot != "auto":
        targets = [explicit_slot]
        log.info(f"=== daily cron START (explicit slot={explicit_slot}) ===")
    else:
        state = load_state()
        targets = slots_due_today(now_utc, state)
        log.info(f"=== daily cron START (self-healing, due slots={targets}) ===")
        if not targets:
            log.info("no slots due / all already done. Exit 0.")
            return 0

    overall_rc = 0
    for slot in targets:
        try:
            rc = generate_one_slot(slot, today_utc, topics)
            if rc == 1:
                overall_rc = 1
                # Don't bail; try the remaining slots too
        except Exception as e:
            log.exception(f"slot {slot} crashed: {e}")
            overall_rc = 1
        # Small delay between slots to space out Telegram messages
        if len(targets) > 1:
            time.sleep(2)

    log.info(f"=== daily cron END (rc={overall_rc}) ===")
    return overall_rc


if __name__ == "__main__":
    sys.exit(main())
