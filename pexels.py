"""
Pexels stock photo search via public HTML scrape.

No API key required. Scrapes the public search page and extracts
image URLs of the form https://images.pexels.com/photos/<id>/pexels-photo-<id>.jpeg

Limits: rate-limited by Pexels CDN; results may vary; HTML structure may change.
For volume use, switch to official Pexels API (free w/ registration).
"""
import logging
import re
import urllib.parse
import urllib.request
from typing import List, Dict

log = logging.getLogger("pexels")

_USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"


def search(query: str, limit: int = 8) -> List[Dict]:
    """
    Search Pexels for `query`. Returns list of dicts with `id` and `url`.
    Each url is a clean Pexels CDN URL (no query params).
    """
    encoded = urllib.parse.quote(query)
    page_url = f"https://www.pexels.com/search/{encoded}/"
    try:
        req = urllib.request.Request(
            page_url, headers={"User-Agent": _USER_AGENT}
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            html = r.read().decode("utf-8", errors="replace")
    except Exception as e:
        log.warning(f"pexels search error: {e}")
        return []

    # Pattern: https://images.pexels.com/photos/<ID>/pexels-photo-<ID>.jpeg
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
    log.info(f"pexels '{query}' → {len(out)} results")
    return out


def download(url: str, dest_path: str, width: int = 1200) -> bool:
    """Download a Pexels image (auto-resized to `width`) to dest_path."""
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
