#!/usr/bin/env python3
"""
One-off generator for the 'I passaggi della lucidatura' carousel.

Searches Pexels for a fresh photo per slide (excluding photos already used by
the daily posts), renders 6 branded slides, and saves them to carousel_out/.
Inspect, then run push_carousel.py to commit + send the Telegram preview.
"""
import json
import os
import random
import sys
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import carousel  # noqa: E402

# --- secrets (local dev fallback; on GH Actions env is provided directly) ---
_sec = HERE / "secrets.env"
if _sec.exists():
    for line in _sec.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

PEXELS_API_KEY = os.environ["PEXELS_API_KEY"]
PEXELS_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

OUT = HERE / "carousel_out"
OUT.mkdir(exist_ok=True)

# --- slide definitions ---
CAROUSEL_ID = "carousel_lucidatura_step_by_step"
SLIDES = [
    {
        "kind": "cover",
        "kicker": "Russo Pavimenti",
        "title1": "I 5 PASSAGGI",
        "title2": "della lucidatura del marmo",
        "queries": ["polished marble floor luxury", "elegant marble interior"],
    },
    {
        "kind": "step", "number": "1", "title": "Molatura",
        "body": "Si rimuove lo strato superficiale rovinato, eliminando graffi e dislivelli.",
        "queries": ["floor grinding machine", "concrete floor grinding", "floor restoration work"],
    },
    {
        "kind": "step", "number": "2", "title": "Levigatura",
        "body": "Abrasivi a grane sempre più fini rendono la superficie liscia e uniforme.",
        "queries": ["marble floor restoration", "floor polishing machine", "stone floor work"],
    },
    {
        "kind": "step", "number": "3", "title": "Lucidatura",
        "body": "La pietra torna a riflettere la luce con la sua brillantezza naturale.",
        "queries": ["shiny marble floor reflection", "glossy marble floor", "polished stone floor"],
    },
    {
        "kind": "step", "number": "4", "title": "Cristallizzazione",
        "body": "Un trattamento crea uno strato lucido e molto più resistente all'usura.",
        "queries": ["luxury marble floor shine", "marble texture elegant", "marble surface detail"],
    },
    {
        "kind": "step", "number": "5", "title": "Protezione",
        "body": "Un sigillante finale protegge dalle macchie e mantiene il risultato nel tempo.",
        "queries": ["clean marble floor home", "modern marble floor", "marble floor living room"],
    },
]
TOTAL_STEPS = "5"


def load_used_photo_ids() -> set:
    """Read daily_state.json from the repo to avoid reusing photos."""
    try:
        d = json.loads((HERE / "daily_state.json").read_text())
        return set(str(k) for k in (d.get("used_photos") or {}).keys())
    except Exception:
        return set()


def pexels_pick(queries, used_ids, picked_now) -> dict:
    """Return {'url','id'} of a fresh photo not in used_ids nor picked_now."""
    pages = [1, 2, 3]
    random.shuffle(pages)
    for q in queries:
        for page in pages:
            url = (f"https://api.pexels.com/v1/search?query={urllib.parse.quote(q)}"
                   f"&per_page=15&page={page}&orientation=square")
            req = urllib.request.Request(url, headers={
                "Authorization": PEXELS_API_KEY,
                "User-Agent": PEXELS_UA, "Accept": "application/json",
            })
            try:
                with urllib.request.urlopen(req, timeout=20) as r:
                    data = json.loads(r.read())
            except Exception as e:
                print(f"  pexels {q!r} p{page} failed: {e}")
                continue
            photos = list(data.get("photos", []) or [])
            random.shuffle(photos)
            for ph in photos:
                pid = str(ph.get("id"))
                if pid in used_ids or pid in picked_now:
                    continue
                src = ph.get("src", {})
                u = src.get("large2x") or src.get("large") or src.get("original")
                if u:
                    print(f"  {q!r} p{page} -> photo {pid}")
                    return {"url": u, "id": pid}
    return {}


def download(url, dest):
    req = urllib.request.Request(url, headers={"User-Agent": PEXELS_UA})
    with urllib.request.urlopen(req, timeout=40) as r:
        Path(dest).write_bytes(r.read())


def main():
    used = load_used_photo_ids()
    print(f"excluding {len(used)} already-used photo ids")
    picked_now = set()
    manifest = []

    for i, slide in enumerate(SLIDES):
        print(f"slide {i}: {slide.get('title', slide.get('title1'))}")
        photo = pexels_pick(slide["queries"], used, picked_now)
        if not photo:
            print(f"  !! no fresh photo for slide {i}, aborting")
            sys.exit(1)
        picked_now.add(photo["id"])
        tmp = OUT / f"_src_{i}.jpg"
        download(photo["url"], tmp)

        out_png = OUT / f"{CAROUSEL_ID}_{i}.png"
        if slide["kind"] == "cover":
            carousel.render_cover(str(tmp), slide["kicker"], slide["title1"],
                                  slide["title2"], str(out_png))
        else:
            carousel.render_step(str(tmp), slide["number"], TOTAL_STEPS,
                                 slide["title"], slide["body"], str(out_png))
        tmp.unlink(missing_ok=True)
        manifest.append({"index": i, "png": out_png.name, "photo_id": photo["id"],
                         "photo_url": photo["url"]})

    (OUT / "manifest.json").write_text(json.dumps(
        {"carousel_id": CAROUSEL_ID, "slides": manifest}, indent=2))
    print(f"\nDONE — {len(manifest)} slides in {OUT}")


if __name__ == "__main__":
    main()
