"""
Carousel renderer for Russo Pavimenti — educational multi-slide Instagram posts.

Produces 1080×1080 PNG slides with a consistent brand look:
  - Full-bleed Pexels stock photo, dark overlay
  - Old Standard TT typography (Romana brand font — never italic)
  - Lime accent (216,255,61) on near-white text
  - Cover slide: big title; step slides: lime numeral + title + wrapped body
  - "MR" monogram + step indicator
"""
import logging
from pathlib import Path
from typing import List, Optional

from PIL import Image, ImageDraw, ImageFont, ImageFilter

log = logging.getLogger("carousel")

HERE = Path(__file__).parent
FONTS_DIR = HERE / "fonts"
ROMAN = str(FONTS_DIR / "OldStandard-Regular.ttf")
ROMAN_B = str(FONTS_DIR / "OldStandard-Bold.ttf")

W, H = 1080, 1080
LIME = (216, 255, 61)
WHITE = (250, 248, 244)
BLACK = (0, 0, 0)


def _font(path: str, size: int):
    return ImageFont.truetype(path, size)


def _fit_photo(path: str) -> Image.Image:
    """Crop-fit photo to (W, H) preserving aspect ratio (center crop)."""
    img = Image.open(path).convert("RGB")
    sw, sh = img.size
    sr, dr = sw / sh, W / H
    if sr > dr:
        nw = int(sh * dr)
        x = (sw - nw) // 2
        img = img.crop((x, 0, x + nw, sh))
    else:
        nh = int(sw / dr)
        y = (sh - nh) // 2
        img = img.crop((0, y, sw, y + nh))
    return img.resize((W, H), Image.LANCZOS)


def _draw_centered(d, text: str, fnt, y: int, fill=WHITE, shadow=True) -> int:
    bb = fnt.getbbox(text)
    x = W // 2 - (bb[2] - bb[0]) // 2 - bb[0]
    if shadow:
        d.text((x + 3, y + 4), text, font=fnt, fill=BLACK)
        d.text((x + 2, y + 3), text, font=fnt, fill=BLACK)
    d.text((x, y), text, font=fnt, fill=fill)
    return y + (bb[3] - bb[1])


def _wrap(text: str, fnt, max_w: int) -> List[str]:
    """Greedy word-wrap to fit max_w pixels."""
    words = text.split()
    lines: List[str] = []
    cur = ""
    for w in words:
        trial = (cur + " " + w).strip()
        bb = fnt.getbbox(trial)
        if bb[2] - bb[0] <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def _base(photo_path: str, overlay_alpha: float) -> Image.Image:
    bg = _fit_photo(photo_path)
    black = Image.new("RGB", (W, H), BLACK)
    return Image.blend(bg, black, max(0.0, min(1.0, overlay_alpha)))


def _backdrop(bg: Image.Image, cy: int, half_h: int, intensity: int = 150) -> Image.Image:
    """Soft dark ellipse behind a text block, centered at cy."""
    mask = Image.new("L", (W, H), 0)
    md = ImageDraw.Draw(mask)
    md.ellipse([W // 2 - 560, cy - half_h, W // 2 + 560, cy + half_h],
               fill=max(0, min(255, intensity)))
    mask = mask.filter(ImageFilter.GaussianBlur(radius=80))
    return Image.composite(Image.new("RGB", (W, H), BLACK), bg, mask)


def _monogram(d) -> None:
    f_mr = _font(ROMAN_B, 36)
    bb = f_mr.getbbox("MR")
    mx = W // 2 - (bb[2] - bb[0]) // 2 - bb[0]
    d.text((mx + 2, H - 62), "MR", font=f_mr, fill=BLACK)
    d.text((mx, H - 64), "MR", font=f_mr, fill=WHITE)


def render_cover(photo_path: str, kicker: str, title1: str, title2: str,
                 output_path: str) -> str:
    """Cover slide: kicker + two-line title + 'scorri' hint."""
    bg = _base(photo_path, overlay_alpha=0.55)
    bg = _backdrop(bg, cy=H // 2, half_h=240, intensity=150)
    d = ImageDraw.Draw(bg)

    # Kicker (letterspaced, lime, small)
    f_k = _font(ROMAN_B, 30)
    spaced = " ".join(kicker.upper())
    _draw_centered(d, spaced, f_k, 300, fill=LIME, shadow=True)

    # Title
    f_t = _font(ROMAN_B, 96)
    f_t2 = _font(ROMAN_B, 70)
    bb1 = f_t.getbbox(title1)
    bb2 = f_t2.getbbox(title2)
    h1 = bb1[3] - bb1[1]
    h2 = bb2[3] - bb2[1]
    GAP = 26
    total = h1 + GAP + h2
    y = (H - total) // 2 + 10
    y = _draw_centered(d, title1, f_t, y, fill=WHITE)
    y = _draw_centered(d, title2, f_t2, y + GAP, fill=LIME)

    # Lime accent line
    lw = int(W * 0.12)
    ly = y + 46
    d.rectangle([(W - lw) // 2, ly, (W + lw) // 2, ly + 4], fill=LIME)

    # Scroll hint
    f_s = _font(ROMAN, 34)
    _draw_centered(d, "Scorri  →", f_s, H - 190, fill=WHITE, shadow=True)

    _monogram(d)
    bg.save(output_path, quality=95)
    log.info(f"cover → {output_path}")
    return output_path


def render_step(photo_path: str, number: str, total: str, title: str,
                body: str, output_path: str) -> str:
    """Step slide: big lime numeral + title + wrapped body + step indicator."""
    bg = _base(photo_path, overlay_alpha=0.52)

    # Measure body wrap first to size the backdrop
    f_body = _font(ROMAN, 41)
    body_lines = _wrap(body, f_body, int(W * 0.78))
    line_h = f_body.getbbox("Ag")[3] - f_body.getbbox("Ag")[1]
    body_block = len(body_lines) * (line_h + 14)

    f_num = _font(ROMAN_B, 150)
    f_title = _font(ROMAN_B, 72)
    num_h = f_num.getbbox(number)[3] - f_num.getbbox(number)[1]
    title_h = f_title.getbbox(title)[3] - f_title.getbbox(title)[1]

    GAP_NT = 40
    GAP_TL = 34
    GAP_LB = 40
    LINE_THICK = 4
    total_h = num_h + GAP_NT + title_h + GAP_TL + LINE_THICK + GAP_LB + body_block
    top = (H - total_h) // 2

    bg = _backdrop(bg, cy=H // 2, half_h=max(240, total_h // 2 + 70), intensity=160)
    d = ImageDraw.Draw(bg)

    y = top
    y = _draw_centered(d, number, f_num, y, fill=LIME)
    y = _draw_centered(d, title, f_title, y + GAP_NT, fill=WHITE)

    # Lime accent line
    lw = int(W * 0.10)
    ly = y + GAP_TL // 2 + 10
    d.rectangle([(W - lw) // 2, ly, (W + lw) // 2, ly + LINE_THICK], fill=LIME)

    # Body
    y = ly + LINE_THICK + GAP_LB
    for ln in body_lines:
        _draw_centered(d, ln, f_body, y, fill=WHITE, shadow=True)
        y += line_h + 14

    # Step indicator bottom
    f_ind = _font(ROMAN_B, 32)
    _draw_centered(d, f"{number} / {total}", f_ind, H - 150, fill=LIME, shadow=True)

    _monogram(d)
    bg.save(output_path, quality=95)
    log.info(f"step {number} → {output_path}")
    return output_path
