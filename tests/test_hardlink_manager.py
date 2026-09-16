"""Tests for hardlink_manager.py — safe hardlink replacement, rollback, batch."""
import os

import pytest

from hardlink_manager import batch_replace_with_hardlinks, is_same_volume, replace_with_hardlink


def make_pair(tmp_path, content=b"identical content" * 100):
    original = tmp_path / "original.bin"
    duplicate = tmp_path / "duplicate.bin"
    original.write_bytes(content)
    duplicate.write_bytes(content)
    return str(original), str(duplicate)


class TestIsSameVolume:
    def test_same_drive_is_same_volume(self):
        assert is_same_volume(r"C:\a\file.bin", r"C:\b\other.bin") is True

    def test_different_drives_are_different_volumes(self):
        assert is_same_volume(r"C:\a\file.bin", r"D:\b\other.bin") is False

    def test_case_insensitive_drive_letters(self):
        assert is_same_volume(r"c:\a\file.bin", r"C:\b\other.bin") is True


class TestReplaceWithHardlink:
    def test_identical_files_linked(self, tmp_path):
        original, duplicate = make_pair(tmp_path)
        ok, err, freed = replace_with_hardlink(original, duplicate)
        assert ok, err
        assert freed == os.path.getsize(duplicate)
        assert os.path.samefile(original, duplicate)
        # Content survives at both paths.
        with open(original, "rb") as f:
            assert f.read() == b"identical content" * 100

    def test_no_temp_files_left_behind(self, tmp_path):
        original, duplicate = make_pair(tmp_path)
        replace_with_hardlink(original, duplicate)
        leftovers = [p for p in os.listdir(tmp_path) if ".tmp_hl_" in p]
        assert leftovers == []

    def test_missing_original_fails(self, tmp_path):
        _, duplicate = make_pair(tmp_path)
        ok, err, freed = replace_with_hardlink(str(tmp_path / "nope.bin"), duplicate)
        assert ok is False
        assert freed == 0
        assert os.path.exists(duplicate)

    def test_missing_duplicate_fails(self, tmp_path):
        original, _ = make_pair(tmp_path)
        ok, err, freed = replace_with_hardlink(original, str(tmp_path / "nope.bin"))
        assert ok is False
        assert freed == 0

    def test_same_path_fails(self, tmp_path):
        original, duplicate = make_pair(tmp_path)
        ok, err, freed = replace_with_hardlink(original, original)
        assert ok is False
        assert "same file" in err.lower()

    def test_already_linked_returns_true_zero_freed(self, tmp_path):
        original, duplicate = make_pair(tmp_path)
        replace_with_hardlink(original, duplicate)
        ok, err, freed = replace_with_hardlink(original, duplicate)
        assert ok is True
        assert freed == 0

    def test_similar_content_refused_c1(self, tmp_path):
        """Regression for C1: a file that merely LOOKS like a duplicate
        (same size, different bytes — e.g. a visually similar photo) must
        never be replaced by a hardlink: that would destroy its content."""
        content_a = b"A" * 1024
        content_b = b"B" * 1024  # same size, different bytes
        original, duplicate = make_pair(tmp_path, content_a)
        with open(duplicate, "wb") as f:
            f.write(content_b)

        ok, err, freed = replace_with_hardlink(original, duplicate)
        assert ok is False
        assert freed == 0
        # The differing duplicate must survive untouched.
        with open(duplicate, "rb") as f:
            assert f.read() == content_b
        assert not os.path.samefile(original, duplicate)

    def test_cross_volume_refused(self, tmp_path, monkeypatch):
        original, duplicate = make_pair(tmp_path)
        real_splitdrive = os.path.splitdrive

        def fake_splitdrive(p):
            drive, rest = real_splitdrive(p)
            if os.path.abspath(p) == os.path.abspath(original):
                return "Q:", rest
            return "R:", rest

        monkeypatch.setattr(os.path, "splitdrive", fake_splitdrive)
        ok, err, freed = replace_with_hardlink(original, duplicate)
        assert ok is False
        assert "volume" in err.lower()
        # Nothing was touched.
        assert os.path.exists(original) and os.path.exists(duplicate)
        assert not os.path.samefile(original, duplicate)

    def test_stale_temp_files_are_cleaned_up_m6(self, tmp_path):
        """Regression for M6: leftover .tmp_hl_* files from a crashed run
        must not block a new hardlink operation."""
        original, duplicate = make_pair(tmp_path)
        # Simulate stale leftovers from a previous crashed attempt.
        pid = os.getpid()
        (tmp_path / f"duplicate.bin.tmp_hl_{pid}").write_bytes(b"stale")
        (tmp_path / f"duplicate.bin.tmp_hl_backup_{pid}").write_bytes(b"stale")

        ok, err, freed = replace_with_hardlink(original, duplicate)
        assert ok, err
        assert freed == os.path.getsize(duplicate)
        assert os.path.samefile(original, duplicate)
        leftovers = [p for p in os.listdir(tmp_path) if ".tmp_hl_" in p]
        assert leftovers == []


class TestBatchReplaceWithHardlinks:
    def test_batch_links_all_duplicates(self, tmp_path):
        original = tmp_path / "orig.bin"
        original.write_bytes(b"DATA" * 50)
        dups = []
        for i in range(3):
            d = tmp_path / f"dup{i}.bin"
            d.write_bytes(b"DATA" * 50)
            dups.append(str(d))

        success, freed, errors, succeeded = batch_replace_with_hardlinks({str(original): dups})
        assert success == 3
        assert errors == []
        assert set(succeeded) == set(dups)
        assert freed == 3 * os.path.getsize(original)
        for d in dups:
            assert os.path.samefile(str(original), d)

    def test_missing_original_reports_error(self, tmp_path):
        d = tmp_path / "dup.bin"
        d.write_bytes(b"X")
        success, freed, errors, succeeded = batch_replace_with_hardlinks(
            {str(tmp_path / "nope.bin"): [str(d)]}
        )
        assert success == 0
        assert len(errors) == 1
        assert "missing" in errors[0].lower()

    def test_mismatched_content_refused_per_file_c1(self, tmp_path):
        original = tmp_path / "orig.bin"
        original.write_bytes(b"RIGHT" * 100)
        good = tmp_path / "good.bin"
        good.write_bytes(b"RIGHT" * 100)
        bad = tmp_path / "bad.bin"
        bad.write_bytes(b"WRONG" * 100)

        success, freed, errors, succeeded = batch_replace_with_hardlinks(
            {str(original): [str(good), str(bad)]}
        )
        assert success == 1
        assert len(errors) == 1
        assert os.path.samefile(str(original), str(good))
        with open(bad, "rb") as f:
            assert f.read() == b"WRONG" * 100
