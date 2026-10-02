"""Reproducible generator for the TwinSweeper application icon.

Design (flat, restrained, matches the app theme colors from ui/components.py):
- rounded-square tile with a vertical indigo -> violet gradient
  (#6366F1 PRIMARY_COLOR -> #8B5CF6 ACCENT_COLOR);
- two slightly offset overlapping rounded squares in white / semi-transparent
  white: the "duplicate pair" metaphor (a file and its copy).

Rendered at 4x supersampling, downscaled with LANCZOS, and saved as a
multi-size .ico (16..256) plus a 256px PNG preview.

Usage (from the project root):
    venv\\Scripts\\python tools\\make_icon.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

# --- design constants -------------------------------------------------------
GRADIENT_TOP = (99, 102, 241)   # #6366F1 (PRIMARY_COLOR)
GRADIENT_BOTTOM = (139, 92, 246)  # #8B5CF6 (ACCENT_COLOR)

BASE_SIZE = 512                 # logical icon canvas
SUPERSAMPLE = 4                 # render at 4x, then downscale (antialiasing)
CANVAS = BASE_SIZE * SUPERSAMPLE

TILE_MARGIN = 0.04              # tile inset from canvas edge (4%)
TILE_RADIUS_RATIO = 0.225       # modern squircle-ish corner radius

CARD_SIZE_RATIO = 0.58          # side of each "duplicate file" card
CARD_RADIUS_RATIO = 0.26        # card corner radius
CARD_OFFSET_RATIO = 0.085       # how far each card shifts from the center
CARD_BACK_ALPHA = 90            # the "copy" behind
CARD_FRONT_ALPHA = 240          # the "file" in front

ICO_SIZES = [16, 32, 48, 64, 128, 256]
PREVIEW_SIZE = 256

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ASSETS_DIR = PROJECT_ROOT / "assets"


def _vertical_gradient(size: int, top: tuple[int, int, int],
                       bottom: tuple[int, int, int]) -> Image.Image:
    """One-pixel-wide column gradient stretched to *size* x *size* (RGB)."""
    column = Image.new("RGB", (1, size))
    for y in range(size):
        t = y / max(size - 1, 1)
        color = tuple(round(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
        column.putpixel((0, y), color)  # type: ignore[arg-type]
    return column.resize((size, size))


def _rounded_mask(size: int, inset: float, radius: float) -> Image.Image:
    """L-mode mask with a rounded rectangle covering [inset, size - inset]."""
    mask = Image.new("L", (size, size), 0)
    draw = ImageDraw.Draw(mask)
    lo, hi = int(size * inset), int(size * (1 - inset))
    draw.rounded_rectangle((lo, lo, hi, hi), radius=int(radius), fill=255)
    return mask


def build_icon(base_size: int = BASE_SIZE, supersample: int = SUPERSAMPLE) -> Image.Image:
    """Render the TwinSweeper icon at *base_size* (RGBA)."""
    canvas = base_size * supersample

    # Gradient tile masked into a rounded square.
    tile_mask = _rounded_mask(
        canvas,
        inset=TILE_MARGIN,
        radius=TILE_RADIUS_RATIO * canvas * (1 - 2 * TILE_MARGIN),
    )
    icon = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    icon.paste(_vertical_gradient(canvas, GRADIENT_TOP, GRADIENT_BOTTOM),
               (0, 0), tile_mask)

    # Two offset overlapping "duplicate file" cards.
    card = int(canvas * CARD_SIZE_RATIO)
    card_radius = int(card * CARD_RADIUS_RATIO)
    offset = int(canvas * CARD_OFFSET_RATIO)
    center = canvas // 2
    overlay = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    for dx, dy, alpha in ((-offset, -offset, CARD_BACK_ALPHA),
                          (offset, offset, CARD_FRONT_ALPHA)):
        lo_x, lo_y = center + dx - card // 2, center + dy - card // 2
        draw.rounded_rectangle(
            (lo_x, lo_y, lo_x + card, lo_y + card),
            radius=card_radius,
            fill=(255, 255, 255, alpha),
        )
    icon = Image.alpha_composite(icon, overlay)

    return icon.resize((base_size, base_size), Image.LANCZOS)


def main() -> None:
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    icon = build_icon()

    ico_path = ASSETS_DIR / "icon.ico"
    icon.save(
        ico_path,
        format="ICO",
        sizes=[(s, s) for s in ICO_SIZES],
    )

    preview_path = ASSETS_DIR / "icon_preview.png"
    icon.resize((PREVIEW_SIZE, PREVIEW_SIZE), Image.LANCZOS).save(preview_path)

    print(f"written: {ico_path}")
    print(f"written: {preview_path}")


if __name__ == "__main__":
    main()
