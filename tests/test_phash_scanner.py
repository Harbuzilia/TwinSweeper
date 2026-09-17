"""Tests for phash_scanner.py — dHash, Hamming distance, clustering."""
import os
import random

import pytest

from phash_scanner import (
    compute_dhash,
    hamming_distance,
    scan_similar_images,
    similarity_percentage,
)


def make_gradient_image(path, size=(256, 256)):
    """Horizontal grayscale gradient — dHash-stable under rescaling."""
    from PIL import Image

    w, h = size
    img = Image.new("L", (w, h))
    px = img.load()
    for y in range(h):
        for x in range(w):
            px[x, y] = (x * 255) // max(1, w - 1)
    img.save(str(path), format="PNG")
    return str(path)


def make_noise_image(path, size=(256, 256), seed=42):
    from PIL import Image

    rng = random.Random(seed)
    w, h = size
    img = Image.new("L", (w, h))
    px = img.load()
    for y in range(h):
        for x in range(w):
            px[x, y] = rng.randrange(256)
    img.save(str(path), format="PNG")
    return str(path)


class TestDHash:
    def test_returns_16_hex_chars(self, tmp_path):
        h = compute_dhash(make_gradient_image(tmp_path / "g.png"))
        assert isinstance(h, str) and len(h) == 16
        int(h, 16)  # parses as hex

    def test_identical_images_same_hash(self, tmp_path):
        p1 = make_gradient_image(tmp_path / "g1.png")
        p2 = make_gradient_image(tmp_path / "g2.png")
        assert compute_dhash(p1) == compute_dhash(p2)

    def test_missing_file_returns_none(self, tmp_path):
        assert compute_dhash(str(tmp_path / "nope.png")) is None

    def test_corrupt_file_returns_none(self, tmp_path):
        p = tmp_path / "corrupt.png"
        p.write_bytes(b"this is not an image")
        assert compute_dhash(str(p)) is None

    def test_gradient_resized_keeps_small_distance(self, tmp_path):
        p1 = make_gradient_image(tmp_path / "big.png", (256, 256))
        p2 = make_gradient_image(tmp_path / "small.png", (128, 128))
        assert hamming_distance(compute_dhash(p1), compute_dhash(p2)) <= 6


class TestHamming:
    def test_identical_hashes_distance_zero(self):
        assert hamming_distance("00ff00ff00ff00ff", "00ff00ff00ff00ff") == 0

    def test_complement_hashes_distance_64(self):
        assert hamming_distance("0" * 16, "f" * 16) == 64

    def test_single_bit_difference(self):
        assert hamming_distance("0000000000000001", "0000000000000000") == 1

    def test_invalid_hex_returns_64(self):
        assert hamming_distance("zzzz", "0000") == 64

    def test_similarity_percentage(self):
        assert similarity_percentage("a" * 16, "a" * 16) == 100.0
        assert similarity_percentage("0" * 16, "f" * 16) == 0.0


class TestScanSimilarImages:
    def test_identical_copies_clustered(self, tmp_path):
        d = tmp_path / "photos"
        d.mkdir()
        make_gradient_image(d / "p1.png")
        make_gradient_image(d / "p2.png")
        make_noise_image(d / "other.png")
        results = scan_similar_images([str(d)])
        assert len(results) == 1
        (files,) = results.values()
        assert {os.path.basename(f.path) for f in files} == {"p1.png", "p2.png"}

    def test_resized_copy_clusters_with_original(self, tmp_path):
        d = tmp_path / "photos"
        d.mkdir()
        make_gradient_image(d / "orig.png", (256, 256))
        make_gradient_image(d / "resized.png", (96, 96))
        results = scan_similar_images([str(d)], similarity_threshold=0.90)
        assert len(results) == 1
        assert len(next(iter(results.values()))) == 2

    def test_different_images_not_clustered(self, tmp_path):
        d = tmp_path / "photos"
        d.mkdir()
        make_noise_image(d / "n1.png", seed=1)
        make_noise_image(d / "n2.png", seed=2)
        assert scan_similar_images([str(d)]) == {}

    def test_group_sorted_oldest_modified_first(self, tmp_path):
        import time

        d = tmp_path / "photos"
        d.mkdir()
        p2 = make_gradient_image(d / "p2.png")
        p1 = make_gradient_image(d / "p1.png")
        now = time.time()
        os.utime(p2, (now - 500, now - 500))
        os.utime(p1, (now, now))
        results = scan_similar_images([str(d)])
        (files,) = results.values()
        assert files[0].path == p2

    def test_duplicate_group_keys_are_unique_m3(self, tmp_path):
        """Regression for M3: two clusters whose first file is named alike
        must not overwrite each other via the group key."""
        da = tmp_path / "dirA"
        db = tmp_path / "dirB"
        da.mkdir()
        db.mkdir()
        # Cluster 1: two identical gradients, first file named photo.png
        make_gradient_image(da / "photo.png")
        make_gradient_image(da / "photo - copy.png")
        # Cluster 2: two identical noise images, first file also named photo.png
        make_noise_image(db / "photo.png", seed=7)
        make_noise_image(db / "photo - copy.png", seed=7)

        results = scan_similar_images([str(da), str(db)])
        all_files = [f.path for files in results.values() for f in files]
        # All four images must survive — the colliding key must not drop a group.
        assert len(all_files) == 4
        assert len(results) == 2

    def test_cancel_flag_returns_empty(self, tmp_path):
        d = tmp_path / "photos"
        d.mkdir()
        make_gradient_image(d / "a.png")
        make_gradient_image(d / "b.png")
        assert scan_similar_images([str(d)], cancel_flag=[True]) == {}

    def test_fewer_than_two_images_returns_empty(self, tmp_path):
        d = tmp_path / "photos"
        d.mkdir()
        make_gradient_image(d / "only.png")
        assert scan_similar_images([str(d)]) == {}

    def test_non_image_files_ignored(self, tmp_path):
        d = tmp_path / "mixed"
        d.mkdir()
        make_gradient_image(d / "a.png")
        (d / "notes.txt").write_text("text file")
        (d / "data.bin").write_bytes(b"\x00" * 100)
        # Only one real image → no groups.
        assert scan_similar_images([str(d)]) == {}

    def test_exclude_patterns_prune_directories(self, tmp_path):
        d = tmp_path / "photos"
        d.mkdir()
        sub = d / "skipme"
        sub.mkdir()
        make_gradient_image(d / "keep.png")
        make_gradient_image(sub / "skip.png")
        results = scan_similar_images([str(d)], exclude_patterns=["skipme"])
        assert results == {}

    def test_strict_threshold_still_groups_identical(self, tmp_path):
        d = tmp_path / "photos"
        d.mkdir()
        make_gradient_image(d / "g1.png", (256, 256))
        make_gradient_image(d / "g2.png", (256, 256))
        make_gradient_image(d / "small.png", (128, 128))
        results = scan_similar_images([str(d)], similarity_threshold=1.0)
        # 100% threshold: byte-identical and scaled copies keep clustering,
        # but nothing else joins.
        assert len(results) == 1
        assert len(next(iter(results.values()))) == 3


class TestDhashExifOrientation:
    """Round 2: phone photos are stored sideways + an EXIF orientation tag,
    while re-saved copies are physically rotated. Without exif_transpose the
    same photo in both forms hashed apart — the main use case silently failed."""

    def test_dhash_matches_exif_rotated_copy(self, tmp_path):
        from PIL import Image

        base = Image.new("L", (80, 50))
        px = base.load()
        for y in range(50):
            for x in range(80):
                px[x, y] = x * 255 // 79

        base.rotate(-90, expand=True).save(str(tmp_path / "physical.jpg"))
        exif = Image.Exif()
        exif[274] = 6  # "stored sideways, rotate 90 CW to display"
        base.save(str(tmp_path / "with_exif.jpg"), exif=exif)

        h_stored = compute_dhash(str(tmp_path / "with_exif.jpg"))
        h_rotated = compute_dhash(str(tmp_path / "physical.jpg"))
        assert h_stored is not None
        assert h_rotated is not None
        assert h_stored == h_rotated


class TestUnsupportedFormatsReported:
    def test_unsupported_image_formats_are_reported(self, tmp_path):
        make_gradient_image(tmp_path / "a.png")
        make_gradient_image(tmp_path / "b.png")
        (tmp_path / "iphone_photo.heic").write_bytes(b"\x00\x01 fake heic")

        messages: list = []
        scan_similar_images([str(tmp_path)], progress_callback=lambda m, p: messages.append(m))

        # "0 groups found" on an iPhone library is not "no duplicates" — the
        # skipped formats must at least be named.
        assert any("unsupported" in m.lower() and ".heic" in m.lower() for m in messages)
