"""Tests for ops_log.py — operation journal, undo, trim, unique ids."""
import os
import shutil
import threading

import pytest

from hardlink_manager import replace_with_hardlink


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
