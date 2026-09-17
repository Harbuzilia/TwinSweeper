"""Round 2 quick win: the thumbnail cache.

ft.Image(src=path) ships the FULL-SIZE file to the flet client — a results
screen with phone photos decoded hundreds of MB for 38px previews. Rows and
grid tiles now feed from small cached JPEGs keyed by (path, mtime).
"""
import os

from PIL import Image

from ui.thumbnails import THUMBS_DIR, get_cached_thumbnail


def make_photo(path, size=(400, 300)):
    img = Image.new("RGB", size)
    px = img.load()
    for y in range(size[1]):
        for x in range(size[0]):
            px[x, y] = (x % 256, y % 256, (x + y) % 256)
    img.save(str(path), format="PNG")
    return str(path)


class TestThumbnailCache:
    def test_creates_small_cached_jpeg(self, tmp_path, monkeypatch):
        monkeypatch.setattr("ui.thumbnails.THUMBS_DIR", str(tmp_path / "thumbs"))
        photo = make_photo(tmp_path / "big.png")

        thumb = get_cached_thumbnail(photo)

        assert thumb != photo
        assert os.path.exists(thumb)
        with Image.open(thumb) as t:
            assert max(t.size) <= 160

    def test_second_call_hits_the_cache(self, tmp_path, monkeypatch):
        monkeypatch.setattr("ui.thumbnails.THUMBS_DIR", str(tmp_path / "thumbs"))
        photo = make_photo(tmp_path / "big.png")
        first = get_cached_thumbnail(photo)
        assert get_cached_thumbnail(photo, os.path.getmtime(photo)) == first

    def test_changed_mtime_regenerates(self, tmp_path, monkeypatch):
        monkeypatch.setattr("ui.thumbnails.THUMBS_DIR", str(tmp_path / "thumbs"))
        photo = make_photo(tmp_path / "big.png")
        first = get_cached_thumbnail(photo)

        future = os.path.getmtime(photo) + 500
        os.utime(photo, (future, future))
        second = get_cached_thumbnail(photo)

        assert second != first
        assert os.path.exists(second)

    def test_corrupt_image_falls_back_to_the_original_path(self, tmp_path, monkeypatch):
        monkeypatch.setattr("ui.thumbnails.THUMBS_DIR", str(tmp_path / "thumbs"))
        broken = tmp_path / "broken.png"
        broken.write_bytes(b"not an image at all")
        assert get_cached_thumbnail(str(broken)) == str(broken)
        assert not os.path.exists(str(tmp_path / "thumbs"))

    def test_default_dir_lives_in_the_app_data_dir(self):
        # The real data dir is redirected by conftest's DUPLICATER_DATA_DIR —
        # thumbnails must never land next to the user's photos.
        assert THUMBS_DIR.endswith("thumbs")
        assert "duplicater" in os.path.basename(os.path.dirname(THUMBS_DIR)).lower()
