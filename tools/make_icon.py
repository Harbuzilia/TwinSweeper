"""Reproducible generator for the TwinSweeper application icon.

Design (flat, restrained, matches the app theme colors from ui/components.py):
- rounded-square tile with a vertical indigo -> violet gradient
  (#6366F1 PRIMARY_COLOR -> #8B5CF6 ACCENT_COLOR), ordered-dithered
  (+/-1, 4x4 Bayer) to prevent 8-bit gradient banding;
- two offset overlapping rounded cards: the "duplicate pair" metaphor.
  The back card ("the copy") is opaque light lavender #E0E7FF (>= 3:1
  WCAG contrast against the tile gradient), the front card ("the file")
  is pure opaque white, and a gradient-colored gap ring around the front
  card keeps the seam between the two cards visible.

Shape masks are rendered at 4x supersampling and downscaled with LANCZOS
(antialiasing); the gradient itself is generated directly at the target
size so the dithering lands at final resolution. Saved as a multi-size
.ico (16..256) plus a 256px PNG preview (the largest .ico frame as-is).

Usage (from the project root):
    venv\\Scripts\\python tools\\make_icon.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageChops, ImageDraw

# --- design constants -------------------------------------------------------
GRADIENT_TOP = (99, 102, 241)   # #6366F1 (PRIMARY_COLOR)
GRADIENT_BOTTOM = (139, 92, 246)  # #8B5CF6 (ACCENT_COLOR)

BASE_SIZE = 512                 # logical design canvas
SUPERSAMPLE = 4                 # render masks at 4x, then downscale (antialiasing)

TILE_MARGIN = 0.07              # tile inset from canvas edge (7%)
TILE_RADIUS_RATIO = 0.225       # modern squircle-ish corner radius

CARD_SIZE_RATIO = 0.60          # side of each "duplicate file" card
CARD_RADIUS_RATIO = 0.26        # card corner radius
CARD_OFFSET_RATIO = 0.07        # how far each card shifts from the center
CARD_BACK_COLOR = (0xE0, 0xE7, 0xFF)  # #E0E7FF: light-lavender "copy" card
CARD_BACK_ALPHA = 255           # opaque: contrast independent of the gradient
CARD_FRONT_ALPHA = 255          # pure white "file" card
SEAM_GAP_PX = 8                 # gradient-colored gap ring around the front
                                # card (8px at BASE_SIZE, 4px in the 256px icon)

ICO_SIZES = [16, 32, 48, 64, 128, 256]

# 4x4 Bayer matrix for ordered dithering (values 0..15).
_BAYER4: tuple[tuple[int, ...], ...] = (
    (0, 8, 2, 10),
    (12, 4, 14, 6),
    (3, 11, 1, 9),
    (15, 7, 13, 5),
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ASSETS_DIR = PROJECT_ROOT / "assets"


def _bayer_noise_masks(size: int) -> tuple[Image.Image, Image.Image]:
    """Two "L"-mode masks (1 = active cell) for +/-1 ordered noise.

    The 4x4 Bayer matrix is tiled with a 4-pixel period, so the noise lands
    on individual pixels (not on size/4 blocks).
    """
    plus_rows: list[list[int]] = []
    minus_rows: list[list[int]] = []
    for brow in _BAYER4:
        rep, rem = divmod(size, 4)
        tiled = list(brow) * rep + list(brow[:rem])
        plus_rows.append([1 if v >= 8 else 0 for v in tiled])
        minus_rows.append([0 if v >= 8 else 1 for v in tiled])
    plus = Image.new("L", (size, size))
    minus = Image.new("L", (size, size))
    plus.putdata([v for y in range(size) for v in plus_rows[y & 3]])
    minus.putdata([v for y in range(size) for v in minus_rows[y & 3]])
    return plus, minus


def _vertical_gradient(size: int, top: tuple[int, int, int],
                       bottom: tuple[int, int, int]) -> Image.Image:
    """Vertical gradient (RGB) with +/-1 ordered dithering (4x4 Bayer).

    Without dithering the 8-bit rounding of the gradient shows as flat
    ~6-7px horizontal bands; the Bayer noise spreads the quantization
    error over a 4px checkerboard instead.
    """
    column = Image.new("RGB", (1, size))
    for y in range(size):
        t = y / max(size - 1, 1)
        color = tuple(round(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
        column.putpixel((0, y), color)  # type: ignore[arg-type]
    img = column.resize((size, size))
    plus, minus = _bayer_noise_masks(size)
    channels = [
        ImageChops.subtract(ImageChops.add(channel, plus), minus)
        for channel in img.split()
    ]
    return Image.merge("RGB", channels)


def _rounded_mask(size: int, inset: float, radius: float) -> Image.Image:
    """L-mode mask with a rounded rectangle covering [inset, size - inset]."""
    mask = Image.new("L", (size, size), 0)
    draw = ImageDraw.Draw(mask)
    lo, hi = int(size * inset), int(size * (1 - inset))
    draw.rounded_rectangle((lo, lo, hi, hi), radius=int(radius), fill=255)
    return mask


def _card_mask(canvas: int, lo: int, side: int, radius: int,
               grow: int = 0) -> Image.Image:
    """L-mode mask of a rounded square with top-left corner (lo, lo).

    With *grow* > 0 the square is dilated by that many pixels (the corner
    radius grows with it), producing a uniform outline ring.
    """
    mask = Image.new("L", (canvas, canvas), 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle(
        (lo - grow, lo - grow, lo + side + grow, lo + side + grow),
        radius=radius + grow,
        fill=255,
    )
    return mask


def build_icon(base_size: int = BASE_SIZE, supersample: int = SUPERSAMPLE) -> Image.Image:
    """Render the TwinSweeper icon at *base_size* (RGBA)."""
    canvas = base_size * supersample
    down = (base_size, base_size)

    # Shape masks are drawn at supersampled resolution and downscaled with
    # LANCZOS: flat fills stay pixel-exact inside, edges stay antialiased.
    tile_mask = _rounded_mask(
        canvas,
        inset=TILE_MARGIN,
        radius=TILE_RADIUS_RATIO * canvas * (1 - 2 * TILE_MARGIN),
    ).resize(down, Image.LANCZOS)

    card = int(canvas * CARD_SIZE_RATIO)
    card_radius = int(card * CARD_RADIUS_RATIO)
    offset = int(canvas * CARD_OFFSET_RATIO)
    gap = max(1, round(canvas * SEAM_GAP_PX / BASE_SIZE))
    center = canvas // 2
    back_lo = center - offset - card // 2
    front_lo = center + offset - card // 2

    back_mask = _card_mask(canvas, back_lo, card, card_radius).resize(
        down, Image.LANCZOS)
    outline_mask = _card_mask(canvas, front_lo, card, card_radius, grow=gap).resize(
        down, Image.LANCZOS)
    front_mask = _card_mask(canvas, front_lo, card, card_radius).resize(
        down, Image.LANCZOS)

    # The gradient is built at final size so the dithering lands 1:1.
    gradient = _vertical_gradient(base_size, GRADIENT_TOP, GRADIENT_BOTTOM)
    back_fill = Image.new("RGBA", down, (*CARD_BACK_COLOR, CARD_BACK_ALPHA))
    front_fill = Image.new("RGBA", down, (255, 255, 255, CARD_FRONT_ALPHA))

    icon = Image.new("RGBA", down, (0, 0, 0, 0))
    icon.paste(gradient, (0, 0), tile_mask)     # gradient tile
    icon.paste(back_fill, (0, 0), back_mask)    # opaque lavender "copy" behind
    icon.paste(gradient, (0, 0), outline_mask)  # tile-colored gap: visible seam
    icon.paste(front_fill, (0, 0), front_mask)  # pure white "file" in front
    return icon


def main() -> None:
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)

    # Master render at the largest .ico entry size: the biggest frame keeps
    # its full-resolution dithering, smaller frames are LANCZOS downscales
    # of it, and the preview PNG is the very same image.
    master = build_icon(max(ICO_SIZES))

    ico_path = ASSETS_DIR / "icon.ico"
    master.save(
        ico_path,
        format="ICO",
        sizes=[(s, s) for s in ICO_SIZES],
    )

    preview_path = ASSETS_DIR / "icon_preview.png"
    master.save(preview_path)

    print(f"written: {ico_path}")
    print(f"written: {preview_path}")


if __name__ == "__main__":
    main()
