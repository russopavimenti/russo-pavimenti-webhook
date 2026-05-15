"""
Pexels stock photo search.

Uses the official Pexels API when PEXELS_API_KEY is configured (preferred —
works from any IP including Render). Falls back to HTML scrape otherwise
(useful for local dev but blocked from many cloud IPs).

Get a free API key: https://www.pexels.com/api/new/ (instant, no credit card).
Free tier: 200 requests/hour.
"""
import json
import logging
import os
import re
import urllib.parse
import urllib.request
from typing import List, Dict

log = logging.getLogger("pexels")

_USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

PEXELS_API_KEY = os.environ.get("PEXELS_API_KEY", "").strip()


def _search_via_api(query: str, limit: int) -> List[Dict]:
    """Official Pexels API — reliable, works from any IP."""
    # No orientation filter — square photos are rare and we crop in renderer anyway.
    url = (
        "https://api.pexels.com/v1/search?"
        f"query={urllib.parse.quote(query)}&per_page={limit}"
    )
    req = urllib.request.Request(
        url, headers={"Authorization": PEXELS_API_KEY},
    )
    log.info(f"pexels API call: {url} (key_present={bool(PEXELS_API_KEY)})")
    print(f"[pexels] API call query={query!r} key_set={bool(PEXELS_API_KEY)}", flush=True)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
    except Exception as e:
        log.warning(f"pexels API error: {e}")
        print(f"[pexels] API ERROR: {e}", flush=True)
        return []

    out: List[Dict] = []
    for photo in data.get("photos", []):
        pid = str(photo.get("id", ""))
        # photo.src has multiple sizes: original, large2x, large, medium, small, portrait, landscape, tiny
        src = photo.get("src", {})
        url_clean = src.get("large") or src.get("medium") or src.get("original", "")
        if pid and url_clean:
            out.append({"id": pid, "url": url_clean})
    log.info(f"pexels API '{query}' → {len(out)} results")
    print(f"[pexels] API '{query}' → {len(out)} results (total_results={data.get('total_results')})", flush=True)
    return out


def _search_via_scrape(query: str, limit: int) -> List[Dict]:
    """Fallback: scrape public search HTML. Blocked from many cloud IPs."""
    encoded = urllib.parse.quote(query)
    page_url = f"https://www.pexels.com/search/{encoded}/"
    try:
        req = urllib.request.Request(
            page_url, headers={"User-Agent": _USER_AGENT}
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            html = r.read().decode("utf-8", errors="replace")
    except Exception as e:
        log.warning(f"pexels scrape error: {e}")
        return []

    pat = re.compile(
        r"https://images\.pexels\.com/photos/(\d+)/pexels-photo-\1\.(jpeg|jpg|png)",
        re.IGNORECASE,
    )
    seen = set()
    out: List[Dict] = []
    for m in pat.finditer(html):
        pid = m.group(1)
        if pid in seen:
            continue
        seen.add(pid)
        out.append({
            "id": pid,
            "url": (
                f"https://images.pexels.com/photos/{pid}/"
                f"pexels-photo-{pid}.jpeg"
            ),
        })
        if len(out) >= limit:
            break
    log.info(f"pexels scrape '{query}' → {len(out)} results")
    return out


def search(query: str, limit: int = 8) -> List[Dict]:
    """Search Pexels. Prefers API if configured, falls back to scrape."""
    if PEXELS_API_KEY:
        return _search_via_api(query, limit)
    return _search_via_scrape(query, limit)


def download(url: str, dest_path: str, width: int = 1200) -> bool:
    """Download a Pexels image to dest_path."""
    # If url already has query params (API returns sized URLs), keep as-is.
    # If it's a clean CDN URL, append size param for compression.
    if "?" in url:
        target = url
    else:
        target = f"{url}?auto=compress&cs=tinysrgb&w={width}"
    try:
        req = urllib.request.Request(
            target, headers={"User-Agent": _USER_AGENT}
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            data = r.read()
        with open(dest_path, "wb") as f:
            f.write(data)
        return True
    except Exception as e:
        log.warning(f"pexels download {url}: {e}")
        return False
