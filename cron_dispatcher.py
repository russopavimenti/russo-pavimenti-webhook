"""
Cron dispatcher — background thread that triggers GH Actions when daily slots
are due.

Why this exists: GitHub Actions free-tier scheduled cron is unreliable — it can
skip entire days of triggers. This module runs INSIDE the Render webhook
server (which is always-on thanks to UptimeRobot pings) and acts as a more
reliable external trigger.

How it works:
  1. Background thread wakes every TICK_SECONDS.
  2. Reads daily_state.json from GitHub.
  3. Computes which slots are scheduled-but-not-done today (self-healing).
  4. If any are due AND no recent dispatch has been made for this slot →
     POST workflow_dispatch to GitHub Actions API.
  5. The triggered GH Action runs daily_post_cron.py which handles
     idempotency, CAS claim, and self-healing recovery.

In-memory dedup:
  _last_dispatch_at = {slot: ts}. We won't re-dispatch the same slot if we
  did so within DISPATCH_COOLDOWN_SECONDS. Survives only as long as the
  Render container is warm; cold start resets it (but the CAS claim on the
  GH Actions side prevents duplicate previews anyway).
"""
import base64
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("cron-dispatcher")

# Slot schedule (UTC). MUST match daily_post_cron.py SLOTS_UTC.
SLOTS_UTC: List[Tuple[str, int, int]] = [
    ("morning", 6, 0),    # 08:00 IT CEST
    ("lunch",   10, 30),  # 12:30 IT CEST
    ("evening", 15, 30),  # 17:30 IT CEST
]
# Grace window: a slot is "due" starting GRACE_MIN before its scheduled time.
GRACE_MIN = 5
# After dispatch, don't re-dispatch the same slot for this many seconds.
# 1 hour is enough — a single dispatch + self-healing covers everything.
DISPATCH_COOLDOWN_SECONDS = 3600
# How often the thread wakes.
TICK_SECONDS = 60


_last_dispatch_at: Dict[str, float] = {}
_lock = threading.Lock()
_thread_started = False


def _gh_headers(token: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _load_daily_state(owner: str, repo: str, branch: str, token: str) -> Optional[Dict[str, Any]]:
    """Fetch daily_state.json from the repo via Contents API."""
    url = (f"https://api.github.com/repos/{owner}/{repo}/contents/"
           f"daily_state.json?ref={branch}")
    req = urllib.request.Request(url, headers=_gh_headers(token))
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            meta = json.loads(r.read())
        if meta.get("encoding") == "base64":
            return json.loads(base64.b64decode(meta["content"]))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {"next_index": 0, "history": []}
        log.warning(f"load_daily_state HTTP {e.code}: {e}")
    except Exception as e:
        log.warning(f"load_daily_state error: {e}")
    return None


def _slot_already_done(state: Dict[str, Any], today_utc: str, slot: str) -> bool:
    for entry in state.get("history", []) or []:
        post_id = entry.get("post_id", "")
        if entry.get("slot") == slot and post_id.startswith(f"daily_{today_utc}_{slot}_"):
            return True
    return False


def compute_due_slots(now_utc: datetime, state: Dict[str, Any]) -> List[str]:
    """Return list of slot names scheduled-but-not-done as of now_utc."""
    today_utc = now_utc.strftime("%Y%m%d")
    due: List[str] = []
    for slot_name, h, m in SLOTS_UTC:
        slot_dt = now_utc.replace(hour=h, minute=m, second=0, microsecond=0)
        if now_utc + timedelta(minutes=GRACE_MIN) < slot_dt:
            continue  # slot in the future
        if _slot_already_done(state, today_utc, slot_name):
            continue  # already done
        due.append(slot_name)
    return due


def _dispatch_workflow(owner: str, repo: str, token: str, slot: str = "auto") -> bool:
    """Fire workflow_dispatch for daily-posts.yml. Returns True on success."""
    url = (f"https://api.github.com/repos/{owner}/{repo}/actions/workflows/"
           f"daily-posts.yml/dispatches")
    body = json.dumps({"ref": "main", "inputs": {"slot": slot}}).encode("utf-8")
    headers = dict(_gh_headers(token))
    headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            # 204 No Content = success
            return r.status == 204
    except urllib.error.HTTPError as e:
        body_err = e.read().decode("utf-8", errors="replace")[:300]
        log.error(f"workflow_dispatch HTTP {e.code}: {body_err}")
        return False
    except Exception as e:
        log.error(f"workflow_dispatch error: {e}")
        return False


def _cooldown_active(slot: str) -> bool:
    last = _last_dispatch_at.get(slot, 0)
    return (time.time() - last) < DISPATCH_COOLDOWN_SECONDS


def tick_once(owner: str, repo: str, branch: str, token: str) -> Dict[str, Any]:
    """One iteration: check state, dispatch if due. Returns a report dict."""
    now = datetime.now(timezone.utc)
    state = _load_daily_state(owner, repo, branch, token)
    if state is None:
        return {"ok": False, "reason": "state load failed"}

    due = compute_due_slots(now, state)
    if not due:
        return {"ok": True, "due": [], "dispatched": False}

    # Skip if cooldown active for ALL due slots
    pending = [s for s in due if not _cooldown_active(s)]
    if not pending:
        return {
            "ok": True,
            "due": due,
            "dispatched": False,
            "reason": "cooldown active for all due slots",
        }

    # One workflow_dispatch call with slot=auto triggers self-healing recovery
    # in daily_post_cron.py, which handles ALL pending slots in sequence.
    ok = _dispatch_workflow(owner, repo, token, slot="auto")
    if ok:
        # Mark cooldown for all currently-due slots so we don't re-dispatch
        # within the cooldown window.
        with _lock:
            now_ts = time.time()
            for s in due:
                _last_dispatch_at[s] = now_ts
        log.info(f"dispatched workflow for due slots={due}")
    else:
        log.warning(f"dispatch failed for due slots={due}")
    return {"ok": ok, "due": due, "dispatched": ok}


def _thread_loop(owner: str, repo: str, branch: str, token: str) -> None:
    log.info("cron-dispatcher thread started")
    while True:
        try:
            tick_once(owner, repo, branch, token)
        except Exception as e:
            log.exception(f"tick error: {e}")
        time.sleep(TICK_SECONDS)


def start(owner: str, repo: str, branch: str, token: str) -> bool:
    """Start the background thread (idempotent)."""
    global _thread_started
    with _lock:
        if _thread_started:
            return False
        if not (owner and repo and token):
            log.warning("cron-dispatcher: missing GH config, NOT starting")
            return False
        t = threading.Thread(
            target=_thread_loop,
            args=(owner, repo, branch, token),
            name="cron-dispatcher",
            daemon=True,
        )
        t.start()
        _thread_started = True
        log.info(f"cron-dispatcher: thread started (owner={owner}/{repo} branch={branch})")
        return True
