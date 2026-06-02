"""
PIL renderer per i daily post Russo Pavimenti.

Astrazione dei daily_NN_*.py script. Produce un PNG 1080×1080:
- Foto stock full-bleed (path locale)
- Overlay scuro configurabile (alpha 0.0–1.0)
- Vignette/backdrop morbido al centro (per leggibilità del testo)
- Due righe d'impatto in Old Standard TT (riga1 bianca, riga2 lime)
- Linea lime decorativa sotto
- Monogramma "MR" piccolo in fondo
"""
import logging
from pathlib import Path
from typing import Tuple

from PIL import Image, ImageDraw, ImageFont, ImageFilter

log = logging.getLogger("renderer")

HERE = Path(__file__).parent
FONTS_DIR = HERE / "fonts"
ROMAN = str(FONTS_DIR / "OldStandard-Regular.ttf")
ROMAN_B = str(FONTS_DIR / "OldStandard-Bold.ttf")

W, H = 1080, 1080
LIME = (216, 255, 61)
WHITE = (250, 248, 244)


def _font(path: str, size: int):
    return ImageFont.truetype(path, size)


def _fit_photo(path: str, y_bias: float = 0.0) -> Image.Image:
    """Crop-fit photo to (W, H) preserving aspect ratio."""
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
        y = max(0, min(sh - nh, y + int(sh * y_bias)))
        img = img.crop((0, y, sw, y + nh))
    return img.resize((W, H), Image.LANCZOS)


def _draw_centered(d, text: str, fnt, y: int, fill=WHITE, shadow=True) -> int:
    bb = fnt.getbbox(text)
    x = W // 2 - (bb[2] - bb[0]) // 2 - bb[0]
    if shadow:
        d.text((x + 3, y + 4), text, font=fnt, fill=(0, 0, 0))
        d.text((x + 2, y + 3), text, font=fnt, fill=(0, 0, 0))
    d.text((x, y), text, font=fnt, fill=fill)
    return y + (bb[3] - bb[1])


def _fit_font_size(text: str, base_size: int, max_width_px: int,
                   font_path: str = ROMAN_B, min_size: int = 36) -> int:
    """Return the largest font size <= base_size that makes `text` fit within
    max_width_px. Falls back to min_size if nothing fits.

    Used for hook lines that may exceed the canvas width — instead of overflowing,
    we scale down the font."""
    size = base_size
    while size > min_size:
        f = ImageFont.truetype(font_path, size)
        bb = f.getbbox(text)
        if (bb[2] - bb[0]) <= max_width_px:
            return size
        size -= 3
    return min_size


def _wrap_to_lines(text: str, fnt, max_width_px: int) -> list:
    """Greedy word-wrap a text into lines that fit max_width_px at the given font."""
    words = text.split()
    if not words:
        return [text]
    lines = []
    cur = ""
    for w in words:
        trial = (cur + " " + w).strip()
        bb = fnt.getbbox(trial)
        if (bb[2] - bb[0]) <= max_width_px or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def render(
    photo_path: str,
    line1: str,
    line2: str,
    output_path: str,
    overlay_alpha: float = 0.50,
    backdrop_intensity: int = 130,
    y_bias: float = 0.0,
    font_size: int = 70,
) -> str:
    """
    Render a 1080×1080 daily post.

    Args:
        photo_path: local path to bg photo
        line1: first text line (rendered in WHITE)
        line2: second text line (rendered in LIME)
        output_path: where to save the PNG
        overlay_alpha: 0=no darkening, 1=fully black. 0.50 = good for dark photos,
            0.18 = good for bright photos. Tune for legibility.
        backdrop_intensity: 0-255, alpha of the soft centered backdrop ellipse.
            Higher = darker background behind text. 130 default.
        y_bias: -0.5..0.5, vertical crop bias for portrait photos.
        font_size: hook font size (default 70pt)

    Returns: the output_path on success.
    """
    bg = _fit_photo(photo_path, y_bias=y_bias)

    # Uniform overlay
    black = Image.new("RGB", (W, H), (0, 0, 0))
    bg = Image.blend(bg, black, max(0.0, min(1.0, overlay_alpha)))

    # Soft backdrop ellipse behind text
    if backdrop_intensity > 0:
        mask = Image.new("L", (W, H), 0)
        md = ImageDraw.Draw(mask)
        cx, cy = W // 2, H // 2
        md.ellipse(
            [cx - 480, cy - 130, cx + 480, cy + 130],
            fill=max(0, min(255, backdrop_intensity)),
        )
        mask = mask.filter(ImageFilter.GaussianBlur(radius=70))
        bg = Image.composite(black, bg, mask)

    d = ImageDraw.Draw(bg)

    # Hook centered vertically.
    # Each line is fitted independently: if it's too wide at `font_size`, we
    # first try a smaller font (down to ~38pt); if even that overflows, we
    # word-wrap into multiple lines. This prevents the "text cut off at the
    # edge" issue observed on long hooks (segnalato 2/6/2026).
    MARGIN = 60                          # left+right safety margin
    MAX_W = W - 2 * MARGIN               # max usable width for hook text

    def _hook_block(text: str, color):
        """Return (lines: list[str], font, line_height)."""
        size = _fit_font_size(text, font_size, MAX_W, font_path=ROMAN_B)
        f = _font(ROMAN_B, size)
        if (f.getbbox(text)[2] - f.getbbox(text)[0]) > MAX_W:
            # still too wide → wrap
            lines = _wrap_to_lines(text, f, MAX_W)
        else:
            lines = [text]
        # line height from font ascent+descent
        asc, desc = f.getmetrics()
        lh = asc + desc
        return lines, f, lh

    lines1, f1, lh1 = _hook_block(line1, WHITE)
    lines2, f2, lh2 = _hook_block(line2, LIME)

    GAP = 12                              # gap between line1 block and line2 block
    INTER_LINE_GAP = 8                    # gap between wrapped lines inside a block
    LIME_OFFSET = 50
    LIME_THICK = 3

    block1_h = lh1 * len(lines1) + INTER_LINE_GAP * (len(lines1) - 1)
    block2_h = lh2 * len(lines2) + INTER_LINE_GAP * (len(lines2) - 1)

    total = block1_h + GAP + block2_h + LIME_OFFSET + LIME_THICK
    y = (H - total) // 2

    for i, ln in enumerate(lines1):
        _draw_centered(d, ln, f1, y, fill=WHITE)
        y += lh1 + (INTER_LINE_GAP if i < len(lines1) - 1 else 0)
    y += GAP
    for i, ln in enumerate(lines2):
        _draw_centered(d, ln, f2, y, fill=LIME)
        y += lh2 + (INTER_LINE_GAP if i < len(lines2) - 1 else 0)

    # Lime accent line
    line_w = int(W * 0.10)
    ly = y + LIME_OFFSET
    d.rectangle(
        [(W - line_w) // 2, ly, (W + line_w) // 2, ly + LIME_THICK],
        fill=LIME,
    )

    # MR monogram bottom center
    f_mr = _font(ROMAN_B, 38)
    bb = f_mr.getbbox("MR")
    mx = W // 2 - (bb[2] - bb[0]) // 2 - bb[0]
    d.text((mx + 2, H - 64), "MR", font=f_mr, fill=(0, 0, 0))
    d.text((mx, H - 66), "MR", font=f_mr, fill=WHITE)

    bg.save(output_path, quality=95)
    log.info(f"rendered → {output_path}")
    return output_path
