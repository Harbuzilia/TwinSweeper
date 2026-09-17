"""Thumbnail cache for photo results.

flet's ``ft.Image(src=path)`` ships the FULL-SIZE file to the client — a
results screen of 50 groups with 8 MP phone photos decodes hundreds of
megabytes for 38px previews. This module keeps small JPEG copies in the
data directory (``thumbs/``), keyed by (path, mtime) so a changed file
regenerates its preview.
"""

import hashlib
import os

from PIL import Image, ImageOps

from db_cache import get_data_dir

THUMB_SIZE = 160  # covers both the 38px row preview and the 150px grid tile
THUMBS_DIR = os.path.join(get_data_dir(), "thumbs")


def get_cached_thumbnail(path: str, mtime: float = None) -> str:
    """Return a small cached JPEG for *path*, generating it on first request.

    Falls back to the original *path* on any failure (corrupt image,
    read-only cache dir): the UI must never break over a preview."""
    try:
        if mtime is None:
            mtime = os.stat(path).st_mtime
        key = hashlib.md5(f"{path}|{mtime}".encode("utf-8", "surrogatepass")).hexdigest()[:24]
        thumb_path = os.path.join(THUMBS_DIR, f"{key}.jpg")
        if os.path.exists(thumb_path):
            return thumb_path

        # Decode first, create the cache dir only when there is something to
        # write — a corrupt image must not even leave an empty thumbs/ behind.
        with Image.open(path) as img:
            img = ImageOps.exif_transpose(img)
            img.thumbnail((THUMB_SIZE, THUMB_SIZE))
            img = img.convert("RGB")
            os.makedirs(THUMBS_DIR, exist_ok=True)
            tmp_path = thumb_path + ".tmp"
            img.save(tmp_path, "JPEG", quality=80)
        os.replace(tmp_path, thumb_path)
        return thumb_path
    except Exception:
        return path
