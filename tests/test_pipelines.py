"""Round 2 / Stage 5: end-to-end tests for the module-level operation
pipelines (perform_delete / perform_hardlink / perform_move) — the exact code
the GUI workers run, including C2 re-verification, H5 trash semantics,
journaling and undo.

Data-safety: every deleted/linked/moved object is a file the test itself
created inside tmp_path.
"""
import os
import time

import main


def snap(path):
    stat = os.stat(path)
    return (path, stat.st_size, stat.st_mtime)


class TestPerformDeletePipeline:
    @staticmethod
    def _make_files(tmp_path):
        for i in (1, 2, 3):
            (tmp_path / f"d{i}.bin").write_bytes(b"A" * 100)
        return [snap(str(tmp_path / f"d{i}.bin")) for i in (1, 2, 3)]

    def test_delete_end_to_end_with_verification(self, tmp_path, monkeypatch, isolated_ops_log):
        entries = self._make_files(tmp_path)
        # d2 changes after the "scan" — C2 must refuse to delete it.
        time.sleep(0.01)
        (tmp_path / "d2.bin").write_bytes(b"A" * 250)
        progress_calls = []
        monkeypatch.setattr(main, "send2trash", os.remove)

        state = main.perform_delete(
            entries, use_trash=True,
            progress_callback=lambda done, tot: progress_calls.append((done, tot)),
        )

        assert state["deleted_count"] == 2
        assert not (tmp_path / "d1.bin").exists()
        assert not (tmp_path / "d3.bin").exists()
        assert (tmp_path / "d2.bin").exists()  # changed → refused
        assert any("d2.bin" in e for e in state["errors"])
        assert state["total_freed"] == 200
        assert progress_calls  # chunk reporting fired

        ops = isolated_ops_log.load_operations()
        assert len(ops) == 1
        assert sorted(ops[0]["paths"]) == sorted([
            str(tmp_path / "d1.bin"), str(tmp_path / "d3.bin")
        ])
        assert ops[0]["details"]["trash"] is True

    def test_trash_failure_never_deletes(self, tmp_path, monkeypatch, isolated_ops_log):
        entries = self._make_files(tmp_path)

        def broken_trash(path):
            raise OSError("recycle bin unavailable")

        monkeypatch.setattr(main, "send2trash", broken_trash)

        state = main.perform_delete(entries, use_trash=True)

        # H5 end to end: a failed Recycle Bin move is an error, never a
        # permanent delete, and nothing lands in the journal.
        assert state["deleted_count"] == 0
        assert all((tmp_path / f"d{i}.bin").exists() for i in (1, 2, 3))
        assert isolated_ops_log.load_operations() == []

    def test_cancelled_run_deletes_nothing(self, tmp_path, monkeypatch, isolated_ops_log):
        entries = self._make_files(tmp_path)
        monkeypatch.setattr(main, "send2trash", os.remove)

        state = main.perform_delete(entries, use_trash=True, cancel_flag=[True])

        assert state["deleted_count"] == 0
        assert all((tmp_path / f"d{i}.bin").exists() for i in (1, 2, 3))


class TestPerformHardlinkPipeline:
    def test_hardlink_end_to_end_with_undo(self, tmp_path, isolated_ops_log):
        orig = tmp_path / "orig.bin"
        dup = tmp_path / "dup.bin"
        orig.write_bytes(b"H" * 300)
        dup.write_bytes(b"H" * 300)
        groups_map = {str(orig): [snap(str(dup))]}

        state = main.perform_hardlink(groups_map)

        assert state["success_count"] == 1
        assert state["freed_bytes"] == 300
        assert os.path.samefile(str(orig), str(dup))

        ops = isolated_ops_log.load_operations()
        # The journal entry (round-1 regression guard): linking without
        # journaling made undo dead.
        assert ops and ops[0]["type"] == "hardlink"
        assert ops[0]["details"]["pairs"] == [[str(orig), str(dup)]]

        restored, errors = isolated_ops_log.undo_hardlink_operation(ops[0]["id"])
        assert restored == 1
        assert not errors
        assert not os.path.samefile(str(orig), str(dup))
        with open(orig, "rb") as f1, open(dup, "rb") as f2:
            assert f1.read() == f2.read()

    def test_changed_file_is_refused_not_linked(self, tmp_path, isolated_ops_log):
        orig = tmp_path / "o.bin"
        dup = tmp_path / "d.bin"
        orig.write_bytes(b"H" * 300)
        dup.write_bytes(b"H" * 300)
        entry = snap(str(dup))
        time.sleep(0.01)
        dup.write_bytes(b"H" * 999)  # changed after the "scan"

        state = main.perform_hardlink({str(orig): [entry]})

        assert state["success_count"] == 0
        assert any("d.bin" in e for e in state["errors"])
        assert not os.path.samefile(str(orig), str(dup))
        assert isolated_ops_log.load_operations() == []


class TestPerformMovePipeline:
    @staticmethod
    def _make_sources(tmp_path):
        src_dir = tmp_path / "src"
        src_dir.mkdir()
        (src_dir / "a.bin").write_bytes(b"AAA")
        (src_dir / "b.bin").write_bytes(b"BBB")
        return [snap(str(src_dir / "a.bin")), snap(str(src_dir / "b.bin"))]

    def test_move_end_to_end_with_undo(self, tmp_path, isolated_ops_log):
        entries = self._make_sources(tmp_path)
        dest = tmp_path / "dest"

        state = main.perform_move(entries, str(dest))

        assert state["moved_count"] == 2
        assert not (tmp_path / "src" / "a.bin").exists()
        assert (dest / "a.bin").read_bytes() == b"AAA"
        assert (dest / "b.bin").read_bytes() == b"BBB"

        ops = isolated_ops_log.load_operations()
        assert ops and ops[0]["type"] == "move"
        assert ops[0]["details"]["destination"] == str(dest)

        restored, errors = isolated_ops_log.undo_move_operation(ops[0]["id"])
        assert restored == 2
        assert not errors
        assert (tmp_path / "src" / "a.bin").read_bytes() == b"AAA"
        assert not (dest / "a.bin").exists()

    def test_move_never_overwrites_existing_destination_file(self, tmp_path, isolated_ops_log):
        entries = self._make_sources(tmp_path)
        dest = tmp_path / "dest"
        dest.mkdir()
        (dest / "a.bin").write_bytes(b"OLD CONTENT")

        state = main.perform_move(entries, str(dest))

        assert state["moved_count"] == 2
        # The pre-existing file is untouched; the newcomer gets "(1)".
        assert (dest / "a.bin").read_bytes() == b"OLD CONTENT"
        assert (dest / "a (1).bin").read_bytes() == b"AAA"

    def test_move_skips_changed_source(self, tmp_path, isolated_ops_log):
        entries = self._make_sources(tmp_path)
        time.sleep(0.01)
        (tmp_path / "src" / "a.bin").write_bytes(b"AAA-CHANGED")

        state = main.perform_move(entries, str(tmp_path / "dest"))

        assert state["moved_count"] == 1  # only b.bin
        assert (tmp_path / "src" / "a.bin").exists()  # refused, still there
        assert any("a.bin" in e for e in state["errors"])
        ops = isolated_ops_log.load_operations()
        assert ops[0]["paths"] == [str(tmp_path / "src" / "b.bin")]

    def test_failed_copy_leaves_source_intact(self, tmp_path, monkeypatch, isolated_ops_log):
        entries = self._make_sources(tmp_path)
        dest = tmp_path / "dest"

        # Force the cross-volume copy path (same-volume moves use an atomic
        # os.replace and never call copy2).
        monkeypatch.setattr(main, "is_same_volume", lambda a, b: False)

        def failing_copy2(src, dst, *a, **kw):
            raise OSError("simulated disk failure")

        monkeypatch.setattr(main.shutil, "copy2", failing_copy2)
        state = main.perform_move(entries, str(dest))

        # The M7 pattern: source survives, no truncated destination file,
        # no journal entry for the failed file, and the reserved placeholder
        # is cleaned up.
        assert state["moved_count"] == 0
        assert (tmp_path / "src" / "a.bin").read_bytes() == b"AAA"
        assert not any(dest.iterdir())
        assert isolated_ops_log.load_operations() == []

    def test_source_removal_failure_after_copy_leaves_no_duplicate(self, tmp_path, monkeypatch, isolated_ops_log):
        """Round 3: if the cross-volume copy succeeds but removing the source
        fails (locked file), the copy must be removed too — otherwise an
        invisible duplicate is left in the destination."""
        entries = self._make_sources(tmp_path)
        dest = tmp_path / "dest"
        monkeypatch.setattr(main, "is_same_volume", lambda a, b: False)

        real_remove = os.remove

        def failing_remove(p):
            # Let temp/placeholder cleanup pass, but fail on the SOURCE files.
            if os.path.basename(p) in ("a.bin", "b.bin") and "src" in p:
                raise OSError("source is locked")
            return real_remove(p)

        monkeypatch.setattr(main.os, "remove", failing_remove)
        state = main.perform_move(entries, str(dest))

        assert state["moved_count"] == 0
        # Sources intact, and the copies were rolled back — no duplicates.
        assert (tmp_path / "src" / "a.bin").read_bytes() == b"AAA"
        assert not any(dest.iterdir())
        assert isolated_ops_log.load_operations() == []

    def test_cancel_midway_still_journals_moved(self, tmp_path, monkeypatch, isolated_ops_log):
        """Round 3 A6: a cancel between chunks must NOT lose the journal for
        files already moved — they have to stay undoable."""
        monkeypatch.setattr(main, "PIPELINE_CHUNK", 1)
        entries = self._make_sources(tmp_path)
        extra = tmp_path / "src" / "c.bin"
        extra.write_bytes(b"CCC")
        entries.append(snap(str(extra)))
        dest = tmp_path / "dest"
        flag = [False]

        def cancel_after_first(done, total):
            if done >= 1:
                flag[0] = True

        state = main.perform_move(entries, str(dest), cancel_flag=flag, progress_callback=cancel_after_first)

        assert state["moved_count"] >= 1
        ops = isolated_ops_log.load_operations()
        assert len(ops) == 1 and ops[0]["type"] == "move"
        assert len(ops[0]["details"]["pairs"]) == state["moved_count"]

    def test_reserve_failure_is_reported_and_pipeline_continues(self, tmp_path, monkeypatch, isolated_ops_log):
        """2.2b: _reserve_destination ran OUTSIDE the per-file try — a single
        failing reservation (read-only destination, disk full) escaped
        perform_move entirely and killed the worker thread instead of being
        reported as a per-file error with the rest of the batch finished."""
        entries = self._make_sources(tmp_path)
        dest = tmp_path / "dest"
        real_reserve = main._reserve_destination

        def reserve_failing_for_a(destination, source_path):
            if os.path.basename(source_path) == "a.bin":
                raise PermissionError("destination is read-only")
            return real_reserve(destination, source_path)

        monkeypatch.setattr(main, "_reserve_destination", reserve_failing_for_a)

        state = main.perform_move(entries, str(dest))

        assert state["moved_count"] == 1  # b.bin still processed
        assert any("a.bin" in e for e in state["errors"])
        assert (tmp_path / "src" / "a.bin").exists()  # refused file intact
        assert (dest / "b.bin").read_bytes() == b"BBB"
        ops = isolated_ops_log.load_operations()
        assert ops and ops[0]["type"] == "move"
        assert ops[0]["paths"] == [str(tmp_path / "src" / "b.bin")]


class TestTrashAvailability:
    def test_trash_unavailable_refuses_permanent_delete(self, tmp_path, monkeypatch, isolated_ops_log):
        """Round 3 A4: 'delete to Recycle Bin' with send2trash missing must
        NEVER silently fall back to a permanent os.remove (H5)."""
        f = tmp_path / "important.bin"
        f.write_bytes(b"DATA" * 10)
        monkeypatch.setattr(main, "HAS_SEND2TRASH", False)

        state = main.perform_delete([snap(str(f))], use_trash=True)

        assert state["deleted_count"] == 0
        assert f.exists()  # the file survives
        assert state["errors"] and "NOT deleted" in state["errors"][0]
        assert isolated_ops_log.load_operations() == []

    def test_cancel_midway_still_journals_deleted(self, tmp_path, monkeypatch, isolated_ops_log):
        """Round 3 A6: a cancel between chunks must NOT lose the journal for
        files already deleted — undo must still work for them."""
        monkeypatch.setattr(main, "PIPELINE_CHUNK", 1)
        files = []
        for i in range(3):
            f = tmp_path / f"f{i}.bin"
            f.write_bytes(b"X" * 10)
            files.append(snap(str(f)))
        flag = [False]
        monkeypatch.setattr(main, "send2trash", os.remove)

        def cancel_after_first(done, total):
            if done >= 1:
                flag[0] = True

        state = main.perform_delete(files, use_trash=True, cancel_flag=flag, progress_callback=cancel_after_first)

        assert state["deleted_count"] >= 1
        # The already-deleted file MUST be journaled despite the cancel.
        ops = isolated_ops_log.load_operations()
        assert len(ops) == 1 and ops[0]["type"] == "delete"
        assert len(ops[0]["paths"]) == state["deleted_count"]


class TestRemoveEmptiedFolders:
    """Round 4: dupeGuru-style "remove emptied folders" — opt-in cleanup of
    folders left empty by delete/move. os.rmdir only: a folder that still
    holds anything is never touched, and the flag defaults to off."""

    def test_delete_removes_emptied_parent(self, tmp_path, isolated_ops_log):
        sub = tmp_path / "only_here"
        sub.mkdir()
        f = sub / "dup.bin"
        f.write_bytes(b"A" * 100)

        state = main.perform_delete([snap(str(f))], use_trash=False, remove_empty_folders=True)

        assert state["deleted_count"] == 1
        assert state["empty_folders_removed"] == 1
        assert not sub.exists()

    def test_delete_keeps_parent_with_remaining_files(self, tmp_path, isolated_ops_log):
        sub = tmp_path / "shared"
        sub.mkdir()
        victim = sub / "dup.bin"
        victim.write_bytes(b"A" * 100)
        keeper = sub / "keep.bin"
        keeper.write_bytes(b"B" * 50)

        state = main.perform_delete([snap(str(victim))], use_trash=False, remove_empty_folders=True)

        assert state["deleted_count"] == 1
        assert state["empty_folders_removed"] == 0
        assert keeper.exists() and sub.exists()

    def test_delete_without_flag_keeps_emptied_parent(self, tmp_path, isolated_ops_log):
        sub = tmp_path / "untouched"
        sub.mkdir()
        f = sub / "dup.bin"
        f.write_bytes(b"A" * 100)

        state = main.perform_delete([snap(str(f))], use_trash=False)

        assert state["deleted_count"] == 1
        assert state["empty_folders_removed"] == 0
        assert sub.exists()  # opt-in: default behaviour unchanged

    def test_move_removes_emptied_source_dir(self, tmp_path, isolated_ops_log):
        src = tmp_path / "src"
        src.mkdir()
        f = src / "a.bin"
        f.write_bytes(b"AAA")
        dest = tmp_path / "dest"

        state = main.perform_move([snap(str(f))], str(dest), remove_empty_folders=True)

        assert state["moved_count"] == 1
        assert state["empty_folders_removed"] == 1
        assert not src.exists()
        assert (dest / "a.bin").exists()

    def test_move_then_undo_recreates_source_folder(self, tmp_path, isolated_ops_log):
        """Undo must restore the file even after its source folder was
        removed — otherwise the new cleanup option would break undo."""
        src = tmp_path / "src_undo"
        src.mkdir()
        f = src / "a.bin"
        f.write_bytes(b"AAA")
        dest = tmp_path / "dest_undo"

        main.perform_move([snap(str(f))], str(dest), remove_empty_folders=True)
        assert not src.exists()

        ops = isolated_ops_log.load_operations()
        assert ops and ops[0]["type"] == "move"
        restored, errors = isolated_ops_log.undo_move_operation(ops[0]["id"])

        assert restored == 1
        assert errors == []
        assert f.exists() and f.read_bytes() == b"AAA"
