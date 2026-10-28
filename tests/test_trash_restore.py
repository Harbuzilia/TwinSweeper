"""Tests for trash_restore.py — Recycle Bin parsing and restore.

Everything runs against a fake ``$Recycle.Bin`` tree inside ``tmp_path``: the
real Recycle Bin of the machine is never read, written or enumerated.

Data-safety rule under test (the mirror image of H5 on the delete path): a
restore that cannot complete NEVER removes the Recycle Bin entry — the bin
file is only unlinked after the restored copy sits at the original path.
"""
import os
from pathlib import Path

import pytest

import trash_restore
from trash_fixtures import make_recycle_index_v1, make_recycle_index_v2, write_recycle_entry


class TestParseIndex:
    def test_v2_index_reads_path_size_and_deletion_time(self):
        parsed = trash_restore.parse_index_bytes(make_recycle_index_v2(r"C:\data\photo.jpg", 1234, 1_700_000_000.0))
        assert parsed is not None
        assert parsed.original_path == r"C:\data\photo.jpg"
        assert parsed.size == 1234
        assert parsed.deleted_ts == pytest.approx(1_700_000_000.0, abs=1)

    def test_v2_index_with_null_terminated_length_has_no_trailing_null(self):
        payload = make_recycle_index_v2(r"C:\data\a.txt", 10, 1_700_000_000.0, null_terminated=True)
        parsed = trash_restore.parse_index_bytes(payload)
        assert parsed is not None
        assert parsed.original_path == r"C:\data\a.txt"

    def test_v1_index_reads_ansi_path(self):
        parsed = trash_restore.parse_index_bytes(make_recycle_index_v1(r"C:\old\legacy.bin", 99, 1_700_000_000.0))
        assert parsed is not None
        assert parsed.original_path == r"C:\old\legacy.bin"
        assert parsed.size == 99

    def test_truncated_index_is_rejected(self):
        assert trash_restore.parse_index_bytes(b"\x02\x00\x00") is None

    def test_index_with_empty_path_is_rejected(self):
        assert trash_restore.parse_index_bytes(make_recycle_index_v2("", 10, 1_700_000_000.0)) is None

    def test_unknown_version_is_rejected(self):
        payload = (7).to_bytes(8, "little", signed=True) + b"\x00" * 40
        assert trash_restore.parse_index_bytes(payload) is None

    def test_garbage_index_file_returns_none(self, tmp_path):
        bad = tmp_path / "$Igarbage.txt"
        bad.write_bytes(b"not a recycle index at all" * 3)
        assert trash_restore.read_index_file(str(bad)) is None


class TestEnumerate:
    def test_iter_entries_pairs_index_and_data(self, fake_recycle_bin, tmp_path):
        original, data = write_recycle_entry(fake_recycle_bin, tmp_path / "target" / "photo.jpg", b"payload")

        entries = list(trash_restore.iter_trash_entries(str(fake_recycle_bin)))

        assert len(entries) == 1
        entry = entries[0]
        assert entry.original_path == original
        assert entry.data_path == data
        assert entry.size == len(b"payload")
        assert entry.is_directory is False

    def test_iter_entries_skips_orphan_data_without_index(self, fake_recycle_bin):
        (fake_recycle_bin / "$RORPHAN.bin").write_bytes(b"no index")
        assert list(trash_restore.iter_trash_entries(str(fake_recycle_bin))) == []

    def test_iter_entries_skips_index_without_data(self, fake_recycle_bin, tmp_path):
        payload = make_recycle_index_v2(str(tmp_path / "gone.txt"), 5, 1_700_000_000.0)
        (fake_recycle_bin / "$ILONELY.txt").write_bytes(payload)
        assert list(trash_restore.iter_trash_entries(str(fake_recycle_bin))) == []

    def test_iter_entries_reads_files_in_the_bin_root(self, tmp_path):
        """Older volumes keep the $I/$R pairs directly in the recycle folder."""
        flat = tmp_path / "flat_bin"
        flat.mkdir()
        write_recycle_entry(flat, tmp_path / "flat.txt", b"x")

        entries = list(trash_restore.iter_trash_entries(str(flat)))

        assert [e.original_path for e in entries] == [str(tmp_path / "flat.txt")]

    def test_build_index_is_keyed_by_normalised_path(self, fake_recycle_bin, tmp_path):
        original, _ = write_recycle_entry(fake_recycle_bin, tmp_path / "dir" / "a.txt", b"abc")

        index = trash_restore.build_index([str(fake_recycle_bin)])

        key = trash_restore.trash_key(original)
        assert key in index
        # A path spelled with a redundant ".." resolves to the same entry.
        noisy = os.path.join(str(tmp_path / "dir"), "..", "dir", "a.txt")
        assert [e.original_path for e in index[trash_restore.trash_key(noisy)]] == [original]

    def test_build_index_prefers_newest_entry(self, fake_recycle_bin, tmp_path):
        original = str(tmp_path / "dup.txt")
        write_recycle_entry(fake_recycle_bin, original, b"old", suffix="OLD1.txt", ts=1_600_000_000.0)
        write_recycle_entry(fake_recycle_bin, original, b"new", suffix="NEW1.txt", ts=1_700_000_000.0)

        index = trash_restore.build_index([str(fake_recycle_bin)])

        entries = index[trash_restore.trash_key(original)]
        assert len(entries) == 2
        assert os.path.basename(entries[0].data_path) == "$RNEW1.txt"


class TestRestore:
    def test_restore_puts_file_back_and_clears_bin_entry(self, fake_recycle_bin, tmp_path):
        target = tmp_path / "target" / "photo.jpg"
        write_recycle_entry(fake_recycle_bin, target, b"payload")

        entry = trash_restore.restore_file(str(target), recycle_dirs=[str(fake_recycle_bin)])

        assert entry.original_path == str(target)
        assert target.read_bytes() == b"payload"
        assert not (fake_recycle_bin / f"$I{Path(entry.data_path).name[2:]}").exists()
        assert list(trash_restore.iter_trash_entries(str(fake_recycle_bin))) == []

    def test_restore_creates_missing_parent_folders(self, fake_recycle_bin, tmp_path):
        target = tmp_path / "deep" / "nested" / "file.bin"
        write_recycle_entry(fake_recycle_bin, target, b"data")

        trash_restore.restore_file(str(target), recycle_dirs=[str(fake_recycle_bin)])

        assert target.read_bytes() == b"data"

    def test_restore_refuses_existing_target_and_keeps_bin_entry(self, fake_recycle_bin, tmp_path):
        target = tmp_path / "target.txt"
        write_recycle_entry(fake_recycle_bin, target, b"from trash")
        target.write_bytes(b"user created this")

        with pytest.raises(trash_restore.TrashPathConflict) as err:
            trash_restore.restore_file(str(target), recycle_dirs=[str(fake_recycle_bin)])

        assert str(target) in str(err.value)
        assert target.read_bytes() == b"user created this"
        assert len(list(trash_restore.iter_trash_entries(str(fake_recycle_bin)))) == 1

    def test_restore_overwrite_replaces_existing_target(self, fake_recycle_bin, tmp_path):
        target = tmp_path / "target.txt"
        write_recycle_entry(fake_recycle_bin, target, b"from trash")
        target.write_bytes(b"stale")

        trash_restore.restore_file(str(target), recycle_dirs=[str(fake_recycle_bin)], overwrite=True)

        assert target.read_bytes() == b"from trash"

    def test_restore_missing_from_bin_raises(self, fake_recycle_bin, tmp_path):
        with pytest.raises(trash_restore.TrashEntryNotFound) as err:
            trash_restore.restore_file(str(tmp_path / "never.txt"), recycle_dirs=[str(fake_recycle_bin)])
        assert str(tmp_path / "never.txt") in str(err.value)

    def test_restore_failure_keeps_bin_entry_and_leaves_no_temp_file(self, fake_recycle_bin, tmp_path, monkeypatch):
        target = tmp_path / "target.txt"
        _original, data = write_recycle_entry(fake_recycle_bin, target, b"payload")

        def failing_copy2(src, dst, *args, **kwargs):
            with open(dst, "wb") as f:
                f.write(b"partial")
            raise OSError("simulated disk failure")

        monkeypatch.setattr(trash_restore.shutil, "copy2", failing_copy2)
        with pytest.raises(trash_restore.TrashRestoreFailed):
            trash_restore.restore_file(str(target), recycle_dirs=[str(fake_recycle_bin)])

        # The bin entry must survive a failed restore (mirror of the H5 rule).
        assert os.path.isfile(data)
        assert len(list(trash_restore.iter_trash_entries(str(fake_recycle_bin)))) == 1
        assert not target.exists()
        assert [p.name for p in tmp_path.iterdir() if "restore" in p.name] == []

    def test_restore_of_directory_entry_is_refused(self, fake_recycle_bin, tmp_path):
        target = tmp_path / "folder"
        data_dir = fake_recycle_bin / "$RDIR1"
        data_dir.mkdir()
        (data_dir / "inner.txt").write_text("x", encoding="utf-8")
        payload = make_recycle_index_v2(str(target), 1, 1_700_000_000.0)
        (fake_recycle_bin / "$IDIR1").write_bytes(payload)

        with pytest.raises(trash_restore.TrashDirectoryUnsupported) as err:
            trash_restore.restore_file(str(target), recycle_dirs=[str(fake_recycle_bin)])

        assert str(target) in str(err.value)
        assert data_dir.is_dir()


class TestBatchHelpers:
    def test_restore_paths_counts_restored_and_reports_errors(self, fake_recycle_bin, tmp_path):
        good = tmp_path / "good.txt"
        missing = tmp_path / "missing.txt"
        write_recycle_entry(fake_recycle_bin, good, b"ok", suffix="GOOD.txt")

        restored, errors = trash_restore.restore_paths([str(good), str(missing)], recycle_dirs=[str(fake_recycle_bin)])

        assert restored == 1
        assert len(errors) == 1
        assert str(missing) in errors[0]
        assert good.read_bytes() == b"ok"

    def test_restorable_paths_lists_only_files_still_in_bin(self, fake_recycle_bin, tmp_path):
        present = tmp_path / "present.txt"
        absent = tmp_path / "absent.txt"
        write_recycle_entry(fake_recycle_bin, present, b"ok")

        restorable = trash_restore.restorable_paths([str(present), str(absent)], recycle_dirs=[str(fake_recycle_bin)])

        assert restorable == {str(present)}

    def test_recycle_bin_dirs_missing_folder_is_ignored(self, tmp_path):
        assert trash_restore.recycle_bin_dirs([str(tmp_path / "nope")]) == []
