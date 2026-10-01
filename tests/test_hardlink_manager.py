"""Tests for hardlink_manager.py — safe hardlink replacement, rollback, batch."""
import os

import pytest

import hardlink_manager
from hardlink_manager import batch_replace_with_hardlinks, is_same_volume, replace_with_hardlink
from scanner import get_file_hash as real_get_file_hash


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

    def test_original_hashed_once_per_group_m5(self, tmp_path, monkeypatch):
        """M5: in a group of 1 original + 5 duplicates the original is hashed
        once (group-level cache) instead of once per pair: 1 + 5 = 6
        get_file_hash calls, not 2 * 5 = 10. The duplicates are always hashed
        fresh — they are the files being replaced."""
        original = tmp_path / "orig.bin"
        original.write_bytes(b"PAYLOAD" * 100)
        dups = []
        for i in range(5):
            d = tmp_path / f"dup{i}.bin"
            d.write_bytes(b"PAYLOAD" * 100)
            dups.append(str(d))

        calls = []

        def counting_get_file_hash(path, *args, **kwargs):
            calls.append(str(path))
            return real_get_file_hash(path, *args, **kwargs)

        monkeypatch.setattr(hardlink_manager, "get_file_hash", counting_get_file_hash)

        success, freed, errors, succeeded = batch_replace_with_hardlinks({str(original): dups})

        assert success == 5
        assert errors == []
        assert len(calls) == 6  # 1 original (cached for the group) + 5 duplicates

    def test_original_rewritten_after_group_hash_not_trusted_m5(self, tmp_path, monkeypatch):
        """M5 invariant: the cached original hash is reused only while its
        stat (size + mtime) is unchanged. Rewriting the original right after
        its group hash was computed invalidates the cache: the hash is
        recomputed, the pair no longer matches and the replacement is
        refused — the duplicate's data survives."""
        original = tmp_path / "orig.bin"
        original.write_bytes(b"OLD" * 100)
        duplicate = tmp_path / "dup.bin"
        duplicate.write_bytes(b"OLD" * 100)

        calls = []

        def rewriting_get_file_hash(path, *args, **kwargs):
            result = real_get_file_hash(path, *args, **kwargs)
            calls.append(str(path))
            if str(path) == str(original) and len(calls) == 1:
                # Substitute the original right after its hash was cached:
                # same size, but a deterministically different mtime.
                with open(original, "wb") as f:
                    f.write(b"NEW" * 100)
                os.utime(original, ns=(10**9, 10**9))
            return result

        monkeypatch.setattr(hardlink_manager, "get_file_hash", rewriting_get_file_hash)

        success, freed, errors, succeeded = batch_replace_with_hardlinks(
            {str(original): [str(duplicate)]}
        )

        assert success == 0  # refused: a stale hash must not green-light the pair
        # The duplicate keeps its own data and is not linked to the changed original.
        with open(duplicate, "rb") as f:
            assert f.read() == b"OLD" * 100
        assert not os.path.samefile(str(original), str(duplicate))
        # The original's hash was recomputed (group hash + revalidation miss).
        assert calls.count(str(original)) == 2


class TestAlreadyLinkedNotJournaled:
    """Round 3 B13: a pre-existing hardlink pair has nothing to free. Adding it
    to succeeded_paths would journal it, and a later 'undo' would unlink files
    the user linked himself long ago."""

    def test_pre_existing_hardlink_skipped(self, tmp_path):
        original = tmp_path / "orig.bin"
        original.write_bytes(b"X" * 50)
        alias = tmp_path / "alias.bin"
        os.link(str(original), str(alias))  # already the same physical file

        success, freed, errors, succeeded = batch_replace_with_hardlinks(
            {str(original): [str(alias)]}
        )

        assert succeeded == []  # nothing journaled
        assert success == 0
        assert os.path.samefile(str(original), str(alias))  # still linked, untouched


class TestOrphanedBackupRecovery:
    """Round 3 B13: a crash between the two renames leaves the duplicate at a
    .tmp_hl_backup_<oldpid> path with nothing at the target. A later run must
    restore it instead of reporting 'duplicate not found' and leaving the file
    invisible at its real path."""

    def test_orphaned_backup_restored_when_target_missing(self, tmp_path):
        original = tmp_path / "orig.bin"
        original.write_bytes(b"DATA" * 20)
        target = tmp_path / "dup.bin"  # does NOT exist (crash removed it)
        orphan = tmp_path / "dup.bin.tmp_hl_backup_99999"  # crashed previous run
        orphan.write_bytes(b"DATA" * 20)

        ok, err, freed = replace_with_hardlink(str(original), str(target))

        assert ok is True
        assert target.exists()  # restored, then linked
        assert not orphan.exists()  # consumed by the restore
        assert os.path.samefile(str(original), str(target))
