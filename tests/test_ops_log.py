"""Tests for ops_log.py — operation journal, undo, trim, unique ids."""
import os
import shutil
import threading

import pytest

from hardlink_manager import replace_with_hardlink
from trash_fixtures import write_recycle_entry
from locales import get_text


class TestAppendAndLoad:
    def test_append_and_load_roundtrip(self, isolated_ops_log):
        op = isolated_ops_log.append_operation("delete", [r"C:\a.txt"], 100)
        ops = isolated_ops_log.load_operations()
        assert len(ops) == 1
        assert ops[0] == op
        assert ops[0]["type"] == "delete"
        assert ops[0]["paths"] == [r"C:\a.txt"]
        assert ops[0]["freed_bytes"] == 100
        assert ops[0]["undone"] is False

    def test_newest_operation_first(self, isolated_ops_log):
        isolated_ops_log.append_operation("delete", [r"C:\a"], 1)
        isolated_ops_log.append_operation("delete", [r"C:\b"], 2)
        ops = isolated_ops_log.load_operations()
        assert ops[0]["paths"] == [r"C:\b"]

    def test_ids_are_unique_for_rapid_appends(self, isolated_ops_log):
        """Regression for L4: id from int(time*1000) collides when two ops
        land in the same millisecond — undo would restore the wrong op."""
        ids = {isolated_ops_log.append_operation("delete", [f"C:\\f{i}"], 1)["id"] for i in range(20)}
        assert len(ids) == 20

    def test_max_operations_trim(self, isolated_ops_log):
        for i in range(105):
            isolated_ops_log.append_operation("delete", [f"C:\\f{i}"], 1)
        ops = isolated_ops_log.load_operations()
        assert len(ops) == isolated_ops_log.MAX_OPERATIONS

    def test_load_missing_file_returns_empty(self, isolated_ops_log):
        assert isolated_ops_log.load_operations() == []

    def test_corrupted_file_returns_empty(self, isolated_ops_log):
        import ops_log

        with open(ops_log.OPS_LOG_FILE, "w", encoding="utf-8") as f:
            f.write("not json at all")
        assert ops_log.load_operations() == []

    def test_log_delete_operation_fields(self, isolated_ops_log):
        op = isolated_ops_log.log_delete_operation([r"C:\a", r"C:\b"], 42, use_trash=True)
        assert op["type"] == "delete"
        assert op["freed_bytes"] == 42
        assert op["details"] == {"trash": True}

    def test_log_hardlink_operation_fields(self, isolated_ops_log):
        op = isolated_ops_log.log_hardlink_operation([(r"C:\orig", r"C:\dup")], 50)
        assert op["type"] == "hardlink"
        assert op["paths"] == [r"C:\dup"]
        assert op["details"]["pairs"] == [[r"C:\orig", r"C:\dup"]]


class TestUndoHardlink:
    def _make_linked_pair(self, tmp_path):
        original = tmp_path / "orig.bin"
        duplicate = tmp_path / "dup.bin"
        content = b"content" * 50
        original.write_bytes(content)
        duplicate.write_bytes(content)
        ok, err, _ = replace_with_hardlink(str(original), str(duplicate))
        assert ok, err
        assert os.path.samefile(str(original), str(duplicate))
        return str(original), str(duplicate)

    def test_undo_restores_separate_copy(self, tmp_path, isolated_ops_log):
        original, duplicate = self._make_linked_pair(tmp_path)
        op = isolated_ops_log.log_hardlink_operation([(original, duplicate)], 100)

        restored, errors = isolated_ops_log.undo_hardlink_operation(op["id"])
        assert restored == 1
        assert errors == []
        assert not os.path.samefile(original, duplicate)
        with open(original, "rb") as f1, open(duplicate, "rb") as f2:
            assert f1.read() == f2.read()

    def test_undo_marks_operation_done(self, tmp_path, isolated_ops_log):
        original, duplicate = self._make_linked_pair(tmp_path)
        op = isolated_ops_log.log_hardlink_operation([(original, duplicate)], 100)
        isolated_ops_log.undo_hardlink_operation(op["id"])
        ops = isolated_ops_log.load_operations()
        assert ops[0]["undone"] is True

    def test_double_undo_refused(self, tmp_path, isolated_ops_log):
        original, duplicate = self._make_linked_pair(tmp_path)
        op = isolated_ops_log.log_hardlink_operation([(original, duplicate)], 100)
        isolated_ops_log.undo_hardlink_operation(op["id"])
        restored, errors = isolated_ops_log.undo_hardlink_operation(op["id"])
        assert restored == 0
        assert errors

    def test_unknown_operation_id(self, isolated_ops_log):
        restored, errors = isolated_ops_log.undo_hardlink_operation("no-such-id")
        assert restored == 0
        assert errors

    def test_undo_non_hardlink_operation_refused(self, isolated_ops_log):
        op = isolated_ops_log.log_delete_operation([r"C:\a"], 1, use_trash=True)
        restored, errors = isolated_ops_log.undo_hardlink_operation(op["id"])
        assert restored == 0
        assert errors

    def test_undo_missing_original_reports_error(self, tmp_path, isolated_ops_log):
        original, duplicate = self._make_linked_pair(tmp_path)
        op = isolated_ops_log.log_hardlink_operation([(original, duplicate)], 100)
        os.remove(original)
        restored, errors = isolated_ops_log.undo_hardlink_operation(op["id"])
        assert restored == 0
        assert errors

    def test_undo_leaves_recreated_file_untouched(self, tmp_path, isolated_ops_log):
        original, duplicate = self._make_linked_pair(tmp_path)
        op = isolated_ops_log.log_hardlink_operation([(original, duplicate)], 100)
        # User recreated the duplicate with new independent content afterwards.
        os.remove(duplicate)
        with open(duplicate, "wb") as f:
            f.write(b"new user content")
        restored, errors = isolated_ops_log.undo_hardlink_operation(op["id"])
        assert restored == 0
        assert errors == []
        with open(duplicate, "rb") as f:
            assert f.read() == b"new user content"

    def test_undo_failure_keeps_duplicate_intact_m7(self, tmp_path, isolated_ops_log, monkeypatch):
        """Regression for M7: if the restore copy fails, the duplicate must
        not vanish — the old code removed it BEFORE copying."""
        original, duplicate = self._make_linked_pair(tmp_path)
        op = isolated_ops_log.log_hardlink_operation([(original, duplicate)], 100)

        def failing_copy2(src, dst, *a, **kw):
            raise OSError("simulated disk failure")

        monkeypatch.setattr(shutil, "copy2", failing_copy2)
        restored, errors = isolated_ops_log.undo_hardlink_operation(op["id"])
        assert restored == 0
        assert errors
        # The duplicate path must still exist and be intact.
        assert os.path.exists(duplicate)
        with open(original, "rb") as f1, open(duplicate, "rb") as f2:
            assert f1.read() == f2.read()

    def test_undo_recreates_deleted_parent_folder(self, tmp_path, isolated_ops_log):
        """2.2a: the duplicate's folder can be gone by undo time (e.g. the
        "remove emptied folders" option ran after the linking) — undo used
        to fail with FileNotFoundError instead of restoring the copy."""
        original = tmp_path / "orig.bin"
        nested = tmp_path / "nested"
        nested.mkdir()
        duplicate = nested / "dup.bin"
        content = b"content" * 50
        original.write_bytes(content)
        duplicate.write_bytes(content)
        ok, err, _ = replace_with_hardlink(str(original), str(duplicate))
        assert ok, err
        op = isolated_ops_log.log_hardlink_operation([(str(original), str(duplicate))], 100)

        os.remove(duplicate)
        nested.rmdir()  # the folder disappeared after the operation

        restored, errors = isolated_ops_log.undo_hardlink_operation(op["id"])

        assert restored == 1
        assert errors == []
        assert duplicate.is_file()
        with open(duplicate, "rb") as f:
            assert f.read() == content


class TestOpsLogConcurrencyAndAtomicity:
    """Round 2: the delete worker and the sweeper worker journal from different
    threads — unlocked load-modify-save cycles silently dropped operations, and
    a crash mid-write truncated the whole undo history."""

    def test_concurrent_appends_never_lose_operations(self, isolated_ops_log):
        def worker(n):
            for i in range(5):
                isolated_ops_log.append_operation("delete", [f"p{n}_{i}"], 0)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        ops = isolated_ops_log.load_operations()
        assert len(ops) == 40
        paths = {p for op in ops for p in op["paths"]}
        assert len(paths) == 40

    def test_crash_during_save_keeps_journal_intact(self, isolated_ops_log, monkeypatch):
        first = isolated_ops_log.append_operation("delete", ["first"], 0)
        assert len(isolated_ops_log.load_operations()) == 1

        def crash_replace(src, dst):
            raise OSError("simulated crash mid-save")

        monkeypatch.setattr("os.replace", crash_replace)
        # Atomic write (tmp + replace) means the failed save leaves the old
        # journal untouched instead of truncating it.
        isolated_ops_log.save_operations([{"id": "lost", "type": "delete", "paths": [], "freed_bytes": 0, "details": {}, "undone": False, "ts": 0.0}])

        ops = isolated_ops_log.load_operations()
        assert len(ops) == 1
        assert ops[0]["id"] == first["id"]


class TestUndoRetryableOnPartialFailure:
    """Round 3 B12: an undo that cannot fully restore must NOT be marked
    'undone' — otherwise the retry button disappears forever and the files
    stay unrestored with no way to try again."""

    def test_undo_with_error_stays_retryable(self, tmp_path, isolated_ops_log):
        original = tmp_path / "orig.bin"
        duplicate = tmp_path / "dup.bin"
        original.write_bytes(b"X" * 50)
        os.link(str(original), str(duplicate))  # real hardlink pair
        op = isolated_ops_log.log_hardlink_operation([(str(original), str(duplicate))], 50)

        # Make restoration impossible: the original is gone.
        os.remove(str(original))
        restored, errors = isolated_ops_log.undo_hardlink_operation(op["id"])

        assert restored == 0
        assert errors  # "Original missing"
        # Crucial: NOT marked undone, so the user can retry.
        assert isolated_ops_log.load_operations()[0]["undone"] is False

    def test_undo_full_success_marks_undone(self, tmp_path, isolated_ops_log):
        original = tmp_path / "orig.bin"
        duplicate = tmp_path / "dup.bin"
        original.write_bytes(b"X" * 50)
        os.link(str(original), str(duplicate))
        op = isolated_ops_log.log_hardlink_operation([(str(original), str(duplicate))], 50)

        restored, errors = isolated_ops_log.undo_hardlink_operation(op["id"])

        assert restored == 1 and not errors
        assert isolated_ops_log.load_operations()[0]["undone"] is True
        # The duplicate is an independent copy again.
        assert not os.path.samefile(str(original), str(duplicate))


class TestUndoDeleteFromTrash:
    """A deletion that went to the Recycle Bin is recoverable through the
    journal's trash flag; an operation deleted permanently is not, and the
    bin entry is never consumed by a restore that did not complete."""

    @pytest.fixture(autouse=True)
    def fake_bin_as_default(self, fake_recycle_bin, monkeypatch):
        """Makes trash_restore look at the fake bin instead of the real one."""
        import trash_restore

        monkeypatch.setattr(trash_restore, "recycle_bin_dirs", lambda roots=None: [str(fake_recycle_bin)])
        return fake_recycle_bin

    def _delete_op(self, ops_log, fake_recycle_bin, tmp_path, *, trash=True, name="victim.txt", content=b"payload"):
        target = tmp_path / "restore_target" / name
        write_recycle_entry(fake_recycle_bin, target, content)
        return target, ops_log.log_delete_operation([str(target)], len(content), use_trash=trash)

    def test_trash_delete_is_restored_and_marked_undone(self, isolated_ops_log, fake_recycle_bin, tmp_path):
        target, op = self._delete_op(isolated_ops_log, fake_recycle_bin, tmp_path)

        restored, errors = isolated_ops_log.undo_delete_operation(op["id"])

        assert restored == 1
        assert errors == []
        assert target.read_bytes() == b"payload"
        assert isolated_ops_log.load_operations()[0]["undone"] is True

    def test_permanent_delete_reports_it_cannot_be_restored(self, isolated_ops_log, fake_recycle_bin, tmp_path):
        target, op = self._delete_op(isolated_ops_log, fake_recycle_bin, tmp_path, trash=False)

        restored, errors = isolated_ops_log.undo_delete_operation(op["id"])

        assert restored == 0
        assert len(errors) == 1
        assert get_text("restore_permanent_delete") in errors[0]
        assert not target.exists()
        # Nothing was restored, so the retry path stays open.
        assert isolated_ops_log.load_operations()[0]["undone"] is False

    def test_file_no_longer_in_bin_reports_error_and_stays_retryable(self, isolated_ops_log, fake_recycle_bin, tmp_path):
        target = tmp_path / "gone.txt"
        op = isolated_ops_log.log_delete_operation([str(target)], 10, use_trash=True)

        restored, errors = isolated_ops_log.undo_delete_operation(op["id"])

        assert restored == 0
        assert errors and str(target) in errors[0]
        assert isolated_ops_log.load_operations()[0]["undone"] is False

    def test_existing_target_is_reported_as_conflict(self, isolated_ops_log, fake_recycle_bin, tmp_path):
        target, op = self._delete_op(isolated_ops_log, fake_recycle_bin, tmp_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"newer user file")

        restored, errors = isolated_ops_log.undo_delete_operation(op["id"])

        assert restored == 0
        assert errors and str(target) in errors[0]
        assert target.read_bytes() == b"newer user file"
        assert isolated_ops_log.load_operations()[0]["undone"] is False

    def test_partial_restore_counts_successes_and_stays_retryable(self, isolated_ops_log, fake_recycle_bin, tmp_path):
        present = tmp_path / "present.txt"
        absent = tmp_path / "absent.txt"
        write_recycle_entry(fake_recycle_bin, present, b"here", suffix="PRESENT.txt")
        op = isolated_ops_log.log_delete_operation([str(present), str(absent)], 20, use_trash=True)

        restored, errors = isolated_ops_log.undo_delete_operation(op["id"])

        assert restored == 1
        assert len(errors) == 1
        assert present.read_bytes() == b"here"
        assert isolated_ops_log.load_operations()[0]["undone"] is False

    def test_double_undo_refused(self, isolated_ops_log, fake_recycle_bin, tmp_path):
        _target, op = self._delete_op(isolated_ops_log, fake_recycle_bin, tmp_path)
        isolated_ops_log.undo_delete_operation(op["id"])

        restored, errors = isolated_ops_log.undo_delete_operation(op["id"])

        assert restored == 0
        assert errors

    def test_undo_delete_refuses_other_operation_types(self, isolated_ops_log, fake_recycle_bin, tmp_path):
        op = isolated_ops_log.log_hardlink_operation([(str(tmp_path / "a"), str(tmp_path / "b"))], 1)

        restored, errors = isolated_ops_log.undo_delete_operation(op["id"])

        assert restored == 0
        assert errors
