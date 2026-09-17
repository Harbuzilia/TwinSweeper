"""Regression tests for the H4-UI sweeper fixes — headless (no page).

Covers: the confirmation dialog before any removal (nothing may be deleted
without it), the Recycle Bin option for file modes, H5 (a failed move to the
Recycle Bin must never fall back to permanent deletion) and journaling into
operations_log. The worker thread is replaced by a synchronous runner so the
outcome is assertable.

Data-safety: every deleted object is a file/folder created by the test itself
inside tmp_path.
"""
import os

import pytest

import ui.sweeper_view as sweeper_view_module
from scanner import FileInfo
from ui.sweeper_view import SweeperView


class _SyncThread:
    """threading.Thread stand-in that runs the worker synchronously."""

    def __init__(self, target=None, daemon=False):
        self._target = target

    def start(self):
        self._target()


class FakePage:
    def __init__(self):
        self.dialogs = []

    def show_dialog(self, dlg):
        self.dialogs.append(dlg)

    def pop_dialog(self):
        return self.dialogs.pop() if self.dialogs else None

    def update(self):
        pass


def make_junk_view(tmp_path, mode="junk_files"):
    view = SweeperView(language="ru")
    junk = tmp_path / "leftover.tmp"
    junk.write_bytes(b"x" * 42)
    view.active_mode = mode
    view.junk_files_list = [FileInfo(str(junk), junk.name, 42, 0.0, 0.0)]
    view.selected_items = {str(junk)}
    return view, junk


class TestConfirmationDialog:
    def test_clean_requires_confirmation(self, tmp_path, monkeypatch):
        """H4: clicking Clean must show a dialog and delete NOTHING yet."""
        view, junk = make_junk_view(tmp_path)
        trashed = []
        monkeypatch.setattr(sweeper_view_module, "send2trash", trashed.append)
        fake_page = FakePage()
        monkeypatch.setattr(SweeperView, "_page_or_none", lambda self: fake_page)

        view.on_clean_clicked(None)

        assert len(fake_page.dialogs) == 1
        assert junk.exists()
        assert trashed == []

    def test_no_selection_is_a_no_op(self, tmp_path, monkeypatch):
        view, _ = make_junk_view(tmp_path)
        view.selected_items.clear()
        fake_page = FakePage()
        monkeypatch.setattr(SweeperView, "_page_or_none", lambda self: fake_page)

        view.on_clean_clicked(None)

        assert fake_page.dialogs == []


class TestTrashDeletion:
    def test_failed_trash_keeps_file(self, tmp_path, monkeypatch):
        """H5: if send2trash raises, the file must survive — the sweeper must
        not silently turn a failed Recycle Bin move into os.remove."""
        view, junk = make_junk_view(tmp_path)

        def broken_trash(path):
            raise OSError("recycle bin unavailable")

        monkeypatch.setattr(sweeper_view_module, "send2trash", broken_trash)
        monkeypatch.setattr(sweeper_view_module.threading, "Thread", _SyncThread)

        view.execute_clean(use_trash=True)

        assert junk.exists()
        # The failed item stays listed so the user can retry it.
        assert [f.path for f in view.junk_files_list] == [str(junk)]

    def test_trash_delete_removes_and_logs(self, tmp_path, monkeypatch, isolated_ops_log):
        view, junk = make_junk_view(tmp_path)

        def fake_trash(path):
            os.remove(path)  # simulate the move out of the folder

        monkeypatch.setattr(sweeper_view_module, "send2trash", fake_trash)
        monkeypatch.setattr(sweeper_view_module.threading, "Thread", _SyncThread)

        view.execute_clean(use_trash=True)

        assert not junk.exists()
        assert view.junk_files_list == []
        ops = isolated_ops_log.load_operations()
        assert ops and ops[0]["type"] == "delete"
        assert ops[0]["details"]["trash"] is True
        assert ops[0]["paths"] == [str(junk)]
        assert ops[0]["freed_bytes"] == 42

    def test_permanent_delete_removes_and_logs(self, tmp_path, monkeypatch, isolated_ops_log):
        view, junk = make_junk_view(tmp_path)
        monkeypatch.setattr(sweeper_view_module.threading, "Thread", _SyncThread)

        view.execute_clean(use_trash=False)

        assert not junk.exists()
        ops = isolated_ops_log.load_operations()
        assert ops and ops[0]["type"] == "delete"
        assert ops[0]["details"]["trash"] is False


class TestEmptyFolderDeletion:
    def test_empty_folder_clean_and_log(self, tmp_path, monkeypatch, isolated_ops_log):
        view = SweeperView(language="ru")
        empty_dir = tmp_path / "empty1"
        empty_dir.mkdir()
        view.active_mode = "empty_folders"
        view.empty_folders_list = [str(empty_dir)]
        view.selected_items = {str(empty_dir)}
        monkeypatch.setattr(sweeper_view_module.threading, "Thread", _SyncThread)

        view.execute_clean(use_trash=True)  # folders ignore the trash flag

        assert not empty_dir.exists()
        assert view.empty_folders_list == []
        ops = isolated_ops_log.load_operations()
        assert ops and ops[0]["type"] == "delete"
        assert ops[0]["details"]["trash"] is False

    def test_nonempty_folder_survives(self, tmp_path, monkeypatch, isolated_ops_log):
        """os.rmdir can only remove truly empty folders — a folder that gained
        a file since the scan must stay and be reported."""
        view = SweeperView(language="ru")
        occupied = tmp_path / "not_empty"
        occupied.mkdir()
        view.active_mode = "empty_folders"
        view.empty_folders_list = [str(occupied)]
        view.selected_items = {str(occupied)}
        monkeypatch.setattr(sweeper_view_module.threading, "Thread", _SyncThread)

        (occupied / "new.txt").write_text("appeared after scan")

        view.execute_clean(use_trash=False)

        assert occupied.exists()
        assert view.empty_folders_list == [str(occupied)]
        assert isolated_ops_log.load_operations() == []


class TestSweepWorkerExecution:
    """Round 2: workers go through page.run_thread when the view is on a page
    (flet context — the H3 fix finally applied to the sweeper too), and the
    scan/clean flow guards against re-entry."""

    def test_run_worker_prefers_page_run_thread(self, tmp_path, monkeypatch):
        view, _ = make_junk_view(tmp_path)
        launched = []

        class RunThreadPage:
            def run_thread(self, fn):
                launched.append(fn)

        monkeypatch.setattr(SweeperView, "_page_or_none", lambda self: RunThreadPage())
        view._run_worker(lambda: None)
        assert len(launched) == 1

    def test_run_worker_falls_back_to_plain_thread(self, tmp_path, monkeypatch):
        view, _ = make_junk_view(tmp_path)
        ran = []
        monkeypatch.setattr(sweeper_view_module.threading, "Thread", _SyncThread)
        # FakePage has no run_thread — the headless fallback must engage.
        monkeypatch.setattr(SweeperView, "_page_or_none", lambda self: FakePage())
        view._run_worker(lambda: ran.append(1))
        assert ran == [1]

    def test_cancel_button_sets_flag_and_hides_itself(self, tmp_path):
        view, _ = make_junk_view(tmp_path)
        view.cancel_sweep_scan(None)
        assert view.cancel_flag[0] is True
        assert view.cancel_button.visible is False

    def test_scan_start_is_blocked_while_busy(self, tmp_path):
        view, _ = make_junk_view(tmp_path)
        view.selected_directories = [str(tmp_path)]
        view._scan_busy = True
        view.status_text.value = "untouched"
        view.start_sweep_scan(None)
        assert view.status_text.value == "untouched"

    def test_full_scan_flow_finds_junk_and_resets_busy(self, tmp_path, monkeypatch):
        (tmp_path / "another.tmp").write_bytes(b"y" * 7)
        view, _ = make_junk_view(tmp_path)
        view.selected_directories = [str(tmp_path)]
        monkeypatch.setattr(sweeper_view_module.threading, "Thread", _SyncThread)
        monkeypatch.setattr(SweeperView, "_page_or_none", lambda self: FakePage())
        monkeypatch.setattr(SweeperView, "update", lambda self: None)

        view.start_sweep_scan(None)

        assert view._scan_busy is False
        assert len(view.junk_files_list) == 2
        assert view.scan_button.visible is True
        assert view.cancel_button.visible is False
