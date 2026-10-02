"""Regression tests for the Этап-2 results_view.py fixes — headless (no page).

Covers C3 (original protection on "select all"/"invert"), M9 (wasted-space
sums actual duplicate sizes, not (n-1) * first file), C1a (hardlink button
hidden for visually-similar results) and C2's UI side (selected entries carry
size/mtime snapshots). The view is constructed without a page: every
self.update() in the touched code paths is guarded.

Data-safety: view models only — no real files are touched at all.
"""
import os
import time

import flet as ft
import pytest

from scanner import FileInfo
from ui.results_view import ResultsView


def make_info(path: str, size: int, modified: float = 1000.0) -> FileInfo:
    return FileInfo(path, os.path.basename(path), size, modified, modified)


def make_view(groups, **kwargs) -> ResultsView:
    """groups: {"key": [FileInfo, ...]} — the shape scan_directory() returns.
    Callbacks are overridable through kwargs."""
    kwargs.setdefault("on_back", lambda: None)
    kwargs.setdefault("on_delete", lambda *a, **k: None)
    return ResultsView(groups, **kwargs)


class TestProtectOriginals:
    def _groups(self, tmp_path):
        g1 = [make_info(str(tmp_path / "g1_0.bin"), 100),
              make_info(str(tmp_path / "g1_1.bin"), 100),
              make_info(str(tmp_path / "g1_2.bin"), 100)]
        g2 = [make_info(str(tmp_path / "g2_0.bin"), 50),
              make_info(str(tmp_path / "g2_1.bin"), 50)]
        return {"hash1": g1, "hash2": g2}

    def test_select_all_keeps_original(self, tmp_path):
        """C3: 'Select All' must never leave a whole group checked — the
        original (first) file of each group is auto-kept."""
        view = make_view(self._groups(tmp_path))
        view.smart_select_dropdown.value = "all"
        view.apply_smart_selection(None)

        for files in view.all_results.values():
            assert files[0].path not in view.selected_paths
            for f in files[1:]:
                assert f.path in view.selected_paths

    def test_invert_keeps_original(self, tmp_path):
        """C3: 'Invert' from an empty selection selects everything — the
        original must still be protected."""
        view = make_view(self._groups(tmp_path))
        view.selected_paths.clear()  # a fresh view pre-selects "keep first" dupes
        view.smart_select_dropdown.value = "invert"
        view.apply_smart_selection(None)

        for files in view.all_results.values():
            assert files[0].path not in view.selected_paths
            assert {f.path for f in files[1:]} <= view.selected_paths

    def test_invert_from_default_selects_only_originals(self, tmp_path):
        """Inverting the default 'keep first' selection checks exactly the
        originals — no group is fully selected, so nothing needs protection."""
        view = make_view(self._groups(tmp_path))
        view.smart_select_dropdown.value = "invert"
        view.apply_smart_selection(None)

        originals = {files[0].path for files in view.all_results.values()}
        assert view.selected_paths == originals

    def test_partial_selection_untouched(self, tmp_path):
        """_protect_originals only acts on FULLY selected groups."""
        view = make_view(self._groups(tmp_path))
        files = view.all_results["hash1"]
        view.selected_paths = {files[1].path, files[2].path}

        view._protect_originals()

        assert view.selected_paths == {files[1].path, files[2].path}

    def test_endangered_groups_counted_then_cleared(self, tmp_path):
        view = make_view(self._groups(tmp_path))
        everything = {f.path for files in view.all_results.values() for f in files}
        view.selected_paths = set(everything)

        assert view.count_endangered_groups() == 2

        view._protect_originals()

        assert view.count_endangered_groups() == 0
        assert len(view.selected_paths) == 3  # 3 dupes kept, 2 originals released


class TestWastedBytes:
    def test_unequal_group_sizes_sum_actual_dupes(self, tmp_path):
        """M9: a group of differently-sized members must count each duplicate's
        real size — the old (n-1) * files[0].size lied."""
        g1 = [make_info(str(tmp_path / "a0.bin"), 100),
              make_info(str(tmp_path / "a1.bin"), 100)]
        g2 = [make_info(str(tmp_path / "b0.bin"), 50),
              make_info(str(tmp_path / "b1.bin"), 80),
              make_info(str(tmp_path / "b2.bin"), 70)]
        view = make_view({"k1": g1, "k2": g2})

        # g1 → 100; g2 → 80 + 70 (not 2 * 50 = 100).
        assert view.total_wasted_bytes == 250

    def test_remove_files_recomputes_wasted(self, tmp_path):
        g = [make_info(str(tmp_path / "a0.bin"), 100),
             make_info(str(tmp_path / "a1.bin"), 100),
             make_info(str(tmp_path / "a2.bin"), 100)]
        view = make_view({"k": g})
        assert view.total_wasted_bytes == 200

        view.remove_files([g[1].path, g[2].path])

        # One file left → no group → nothing wasted.
        assert view.total_wasted_bytes == 0
        assert g[1].path not in view._path_to_info


class TestHardlinkVisibility:
    def test_hidden_for_similar_image_results(self, tmp_path):
        """C1a: pHash results are similar, not byte-identical — hardlinking
        them would corrupt content, so the button must be hidden."""
        g = [make_info(str(tmp_path / "p0.jpg"), 10),
             make_info(str(tmp_path / "p1.jpg"), 10)]
        view = make_view({"Photo Group: 0": g}, allow_hardlink=False)
        assert view.hardlink_btn.visible is False

    def test_visible_for_identical_results(self, tmp_path):
        g = [make_info(str(tmp_path / "a0.bin"), 100),
             make_info(str(tmp_path / "a1.bin"), 100)]
        view = make_view({"hash": g})
        assert view.hardlink_btn.visible is True


class TestGetSelectedEntries:
    def test_entries_carry_size_and_mtime_snapshots(self, tmp_path):
        """C2: the delete handler needs the (path, size, mtime) captured at
        scan time to re-verify each file before removing it."""
        g = [make_info(str(tmp_path / "a0.bin"), 120, 111.0),
             make_info(str(tmp_path / "a1.bin"), 130, 222.5)]
        view = make_view({"k": g})
        view.selected_paths = {g[1].path}

        entries = view.get_selected_entries()

        assert entries == [(g[1].path, 130, 222.5)]

    def test_unknown_path_is_skipped(self, tmp_path):
        g = [make_info(str(tmp_path / "a0.bin"), 120)]
        view = make_view({"k": [g[0], make_info(str(tmp_path / "a1.bin"), 120)]})
        view.selected_paths = {str(tmp_path / "not_in_results.bin")}

        assert view.get_selected_entries() == []


class TestPhashNoPreselection:
    """Round 2: similar photos are NOT byte-identical duplicates — pre-selecting
    all-but-one for deletion invited a one-click loss of genuinely different
    shots. Regular duplicate results keep the convenient pre-selection."""

    def test_similar_photos_not_preselected(self, tmp_path):
        g = [make_info(str(tmp_path / "p0.jpg"), 10),
             make_info(str(tmp_path / "p1.jpg"), 10),
             make_info(str(tmp_path / "p2.jpg"), 10)]
        view = make_view({"Photo Group: 0": g}, allow_hardlink=False)
        assert view.selected_paths == set()

    def test_duplicates_still_preselected_by_default(self, tmp_path):
        g = [make_info(str(tmp_path / "a0.bin"), 10),
             make_info(str(tmp_path / "a1.bin"), 10)]
        view = make_view({"k": g})
        assert view.selected_paths == {g[1].path}


class TestContentVerifiedGuard:
    """Round 3 A5: a size/name-only scan never compared content, so its groups
    must NOT be preselected for deletion (one click could erase content-different
    files), and a warning banner must be shown."""

    def test_unverified_scan_not_preselected(self, tmp_path):
        g = [make_info(str(tmp_path / "a0.bin"), 10),
             make_info(str(tmp_path / "a1.bin"), 10)]
        view = make_view({"k": g}, content_verified=False)
        assert view.selected_paths == set()

    def test_verified_scan_is_preselected(self, tmp_path):
        g = [make_info(str(tmp_path / "a0.bin"), 10),
             make_info(str(tmp_path / "a1.bin"), 10)]
        view = make_view({"k": g}, content_verified=True)
        assert view.selected_paths == {g[1].path}

    def test_warning_banner_visibility_follows_flag(self, tmp_path):
        g = [make_info(str(tmp_path / "a0.bin"), 10)]
        unverified = make_view({"k": g}, content_verified=False)
        verified = make_view({"k": g}, content_verified=True)
        assert unverified.unverified_banner.visible is True
        assert verified.unverified_banner.visible is False


class TestZeroByteNoPreselect:
    """Round 3: 0-byte 'duplicates' (__init.py, .gitkeep, lock files) free
    nothing and their mass removal breaks projects — never pre-select them."""

    def test_zero_byte_group_not_preselected(self, tmp_path):
        g = [make_info(str(tmp_path / "e0"), 0), make_info(str(tmp_path / "e1"), 0)]
        view = make_view({"k": g})
        assert view.selected_paths == set()

    def test_nonzero_group_still_preselected(self, tmp_path):
        g = [make_info(str(tmp_path / "a0.bin"), 10), make_info(str(tmp_path / "a1.bin"), 10)]
        view = make_view({"k": g})
        assert view.selected_paths == {g[1].path}


class TestOperationBusyGuard:
    """Round 2: Delete/Hardlink buttons must be no-ops while their worker is
    already running — a second click used to launch a second worker over the
    same selection."""

    def test_busy_blocks_delete(self, tmp_path):
        calls = []
        g = [make_info(str(tmp_path / "a0.bin"), 10),
             make_info(str(tmp_path / "a1.bin"), 10)]
        view = make_view({"k": g}, on_delete=lambda *a, **k: calls.append(a))
        view.selected_paths = {g[1].path}
        view._operation_busy = True
        view.on_delete_clicked(None)
        assert calls == []

    def test_busy_blocks_hardlink(self, tmp_path):
        calls = []
        g = [make_info(str(tmp_path / "a0.bin"), 10),
             make_info(str(tmp_path / "a1.bin"), 10)]
        view = make_view({"k": g}, on_hardlink=lambda *a, **k: calls.append(a))
        view.selected_paths = {g[1].path}
        view._operation_busy = True
        view.on_hardlink_clicked(None)
        assert calls == []


class TestSmartSelectRules:
    """Round 2 / Stage 4: 'keep newest / oldest / highest resolution /
    priority-folder copy' selection rules."""

    @staticmethod
    def _apply(view, rule):
        view.smart_select_dropdown.value = rule
        view.apply_smart_selection(None)

    def test_keep_newest(self, tmp_path):
        old = make_info(str(tmp_path / "old.bin"), 10, modified=1000.0)
        new = make_info(str(tmp_path / "new.bin"), 10, modified=2000.0)
        view = make_view({"k": [old, new]})
        view.selected_paths.clear()
        self._apply(view, "newest")
        assert view.selected_paths == {old.path}

    def test_keep_oldest(self, tmp_path):
        old = make_info(str(tmp_path / "old.bin"), 10, modified=1000.0)
        new = make_info(str(tmp_path / "new.bin"), 10, modified=2000.0)
        view = make_view({"k": [old, new]})
        view.selected_paths.clear()
        self._apply(view, "oldest")
        assert view.selected_paths == {new.path}

    def test_keep_largest_resolution(self, tmp_path):
        from PIL import Image

        Image.new("RGB", (60, 40)).save(str(tmp_path / "small.png"))
        Image.new("RGB", (800, 600)).save(str(tmp_path / "big.png"))
        small = make_info(str(tmp_path / "small.png"), 10)
        big = make_info(str(tmp_path / "big.png"), 10)
        view = make_view({"k": [small, big]})
        view.selected_paths.clear()
        self._apply(view, "largest_res")
        assert view.selected_paths == {small.path}

    def test_priority_rule_keeps_priority_folder_copies(self, tmp_path, monkeypatch):
        keep_dir = tmp_path / "keepme"
        keep_dir.mkdir()
        others_dir = tmp_path / "cleanup"
        others_dir.mkdir()
        keeper = make_info(str(keep_dir / "a.bin"), 10)
        dup1 = make_info(str(others_dir / "a.bin"), 10)
        dup2 = make_info(str(others_dir / "a (2).bin"), 10)
        view = make_view({"k": [keeper, dup1, dup2]})
        view.selected_paths.clear()

        import ui.results_view as results_view_module
        monkeypatch.setattr(results_view_module, "load_priority_folders", lambda: [str(keep_dir)])

        self._apply(view, "priority")
        assert view.selected_paths == {dup1.path, dup2.path}

    def test_priority_rule_without_match_keeps_first(self, tmp_path, monkeypatch):
        d1 = tmp_path / "d1"
        d2 = tmp_path / "d2"
        d1.mkdir()
        d2.mkdir()
        f0 = make_info(str(d1 / "a.bin"), 10)
        f1 = make_info(str(d2 / "a.bin"), 10)
        view = make_view({"k": [f0, f1]})
        view.selected_paths.clear()

        import ui.results_view as results_view_module
        monkeypatch.setattr(results_view_module, "load_priority_folders", lambda: [])

        self._apply(view, "priority")
        assert view.selected_paths == {f1.path}


class TestMoveProtectsOriginal:
    """Round 3 B10: 'Move to folder' must also guarantee ≥1 copy stays — a
    fully selected group must not be moved out entirely (the delete path had
    this C3 guard; move did not)."""

    def test_move_keeps_one_copy_when_all_selected(self, tmp_path):
        g = [make_info(str(tmp_path / "a0.bin"), 10),
             make_info(str(tmp_path / "a1.bin"), 10)]
        moves = []
        view = make_view({"k": g}, on_move=lambda entries, dest, **kw: moves.append((entries, dest)))
        view.selected_paths = {g[0].path, g[1].path}  # user selected ALL

        view._do_move(str(tmp_path / "dest"))

        assert len(moves) == 1
        entries, dest = moves[0]
        moved_paths = {e[0] for e in entries}
        # The original (a0, oldest) is protected — only a1 is moved.
        assert moved_paths == {g[1].path}
        assert dest == str(tmp_path / "dest")

    def test_partial_selection_moves_as_is(self, tmp_path):
        g = [make_info(str(tmp_path / "a0.bin"), 10),
             make_info(str(tmp_path / "a1.bin"), 10),
             make_info(str(tmp_path / "a2.bin"), 10)]
        moves = []
        view = make_view({"k": g}, on_move=lambda entries, dest, **kw: moves.append(entries))
        view.selected_paths = {g[2].path}  # only one, original safe

        view._do_move(str(tmp_path / "dest"))

        assert {e[0] for e in moves[0]} == {g[2].path}


class TestTrashAvailabilityGating:
    """Round 3 A4: when send2trash is unavailable, the 'to Recycle Bin'
    checkbox must be disabled and unchecked — confirming the dialog then
    explicitly means permanent deletion, never a silent fallback."""

    @staticmethod
    def _dialog_checkbox(view):
        class FakePage:
            def __init__(self):
                self.dialogs = []

            def show_dialog(self, d):
                self.dialogs.append(d)

            def pop_dialog(self):
                return self.dialogs.pop() if self.dialogs else None

        page = FakePage()
        view._page_or_none = lambda: page
        view.on_delete_clicked(None)
        assert len(page.dialogs) == 1
        # The trash checkbox is the last control of the dialog content column.
        return page.dialogs[0].content.controls[-1]

    def test_checkbox_disabled_when_trash_unavailable(self, tmp_path):
        g = [make_info(str(tmp_path / "a0.bin"), 10),
             make_info(str(tmp_path / "a1.bin"), 10)]
        view = make_view({"k": g}, trash_available=False, trash_default=True)
        cb = self._dialog_checkbox(view)
        assert cb.disabled is True
        assert cb.value is False

    def test_checkbox_enabled_when_trash_available(self, tmp_path):
        g = [make_info(str(tmp_path / "a0.bin"), 10),
             make_info(str(tmp_path / "a1.bin"), 10)]
        view = make_view({"k": g}, trash_available=True, trash_default=True)
        cb = self._dialog_checkbox(view)
        assert cb.disabled is False
        assert cb.value is True


class TestExportFormats:
    """All four export formats, checked by real output structure — not just
    'some substring is present'."""

    @staticmethod
    def _view(tmp_path):
        g = [make_info(str(tmp_path / "a0.bin"), 100),
             make_info(str(tmp_path / "a1.bin"), 200)]
        return make_view({"group-key": g})

    def test_html_report_structure(self, tmp_path):
        view = self._view(tmp_path)
        target = tmp_path / "report.html"
        view.write_export_file(str(target), "html")
        content = target.read_text(encoding="utf-8")
        assert content.startswith("<!DOCTYPE html>")
        # Group header row + both file rows + reclaimable (= all but first).
        assert "<tr class='group'>" in content
        assert "group-key" in content
        assert "a0.bin" in content and "a1.bin" in content
        assert "200" in content  # reclaimable bytes = a1.bin size

    def test_csv_has_bom_for_excel(self, tmp_path):
        view = self._view(tmp_path)
        target = tmp_path / "report.csv"
        view.write_export_file(str(target), "csv")
        raw = target.read_bytes()
        # utf-8-sig BOM — without it Excel opens Cyrillic paths as garbage.
        assert raw.startswith(b"\xef\xbb\xbf")
        text = raw.decode("utf-8-sig")
        assert text.splitlines()[0].startswith("Group,File Name")
        assert "a0.bin" in text

    def test_json_schema(self, tmp_path):
        import json
        view = self._view(tmp_path)
        target = tmp_path / "report.json"
        view.write_export_file(str(target), "json")
        data = json.loads(target.read_text(encoding="utf-8"))
        assert data["total_groups"] == 1
        assert data["groups"]["group-key"]
        assert len(data["groups"]["group-key"]) == 2

    def test_txt_report(self, tmp_path):
        view = self._view(tmp_path)
        target = tmp_path / "report.txt"
        view.write_export_file(str(target), "txt")
        text = target.read_text(encoding="utf-8")
        assert "TWINSWEEPER REPORT" in text
        assert "a0.bin" in text

    def test_export_failure_is_logged_not_swallowed_silently(self, tmp_path, caplog):
        import logging
        view = make_view({"k": [make_info(str(tmp_path / "a.bin"), 10)]})
        with caplog.at_level(logging.WARNING):
            # A directory as the target file: open() fails.
            view.write_export_file(str(tmp_path), "txt")
        # Swallowed for the UI, but it MUST surface in the log.
        assert any("Export error" in r.message for r in caplog.records)


class TestRemoveEmptyFoldersOption:
    """Round 4: 'Remove emptied folders' is opt-in (default off) in both the
    delete and the move confirm dialogs, and its value reaches the callbacks."""

    @staticmethod
    def _fake_page():
        class FakePage:
            def __init__(self):
                self.dialogs = []

            def show_dialog(self, d):
                self.dialogs.append(d)

            def pop_dialog(self):
                return self.dialogs.pop() if self.dialogs else None

        return FakePage()

    @staticmethod
    def _remove_empty_checkbox(dialog):
        """The new checkbox — identified by its tooltip, not by position
        (warning banners may be inserted into the same content column)."""
        return next(c for c in dialog.content.controls if isinstance(c, ft.Checkbox) and c.tooltip)

    def test_delete_dialog_default_off_and_forwarded(self, tmp_path):
        g = [make_info(str(tmp_path / "a0.bin"), 10),
             make_info(str(tmp_path / "a1.bin"), 10)]
        calls = []
        view = make_view({"k": g}, on_delete=lambda *a, **k: calls.append(k))
        view.selected_paths = {g[1].path}
        page = self._fake_page()
        view._page_or_none = lambda: page

        view.on_delete_clicked(None)
        dialog = page.dialogs[0]
        assert self._remove_empty_checkbox(dialog).value is False  # opt-in

        dialog.actions[-1].on_click(None)  # confirm
        assert len(calls) == 1
        assert calls[0].get("remove_empty_folders") is False

    def test_delete_dialog_checked_flag_forwarded(self, tmp_path):
        g = [make_info(str(tmp_path / "a0.bin"), 10),
             make_info(str(tmp_path / "a1.bin"), 10)]
        calls = []
        view = make_view({"k": g}, on_delete=lambda *a, **k: calls.append(k))
        view.selected_paths = {g[1].path}
        page = self._fake_page()
        view._page_or_none = lambda: page

        view.on_delete_clicked(None)
        dialog = page.dialogs[0]
        self._remove_empty_checkbox(dialog).value = True

        dialog.actions[-1].on_click(None)
        assert calls[0].get("remove_empty_folders") is True

    def test_move_forwards_remove_empty_flag(self, tmp_path):
        g = [make_info(str(tmp_path / "a0.bin"), 10),
             make_info(str(tmp_path / "a1.bin"), 10)]
        calls = []
        view = make_view({"k": g}, on_move=lambda entries, dest, **kw: calls.append(kw))
        view.selected_paths = {g[1].path}

        view._do_move(str(tmp_path / "dest"), remove_empty=True)
        assert calls == [{"remove_empty_folders": True}]

    def test_move_default_flag_is_off(self, tmp_path):
        g = [make_info(str(tmp_path / "a0.bin"), 10),
             make_info(str(tmp_path / "a1.bin"), 10)]
        calls = []
        view = make_view({"k": g}, on_move=lambda entries, dest, **kw: calls.append(kw))
        view.selected_paths = {g[1].path}

        view._do_move(str(tmp_path / "dest"))
        assert calls == [{"remove_empty_folders": False}]


class TestSearchDebounce:
    """2.1c: on_search_change rebuilt and redrew the whole results column on
    EVERY keystroke — an O(groups) render storm while typing. The refresh is
    now deferred ~250 ms after the LAST keystroke; a newer keystroke cancels
    (supersedes) the pending refresh."""

    DELAY = 0.05  # instance-level override of SEARCH_DEBOUNCE_DELAY

    def _view(self, tmp_path, monkeypatch):
        g = [make_info(str(tmp_path / "a0.bin"), 10),
             make_info(str(tmp_path / "a1.bin"), 10)]
        view = make_view({"k": g})
        view.SEARCH_DEBOUNCE_DELAY = self.DELAY
        return view

    def test_refresh_deferred_until_typing_pauses(self, tmp_path, monkeypatch):
        view = self._view(tmp_path, monkeypatch)
        refreshes = []
        monkeypatch.setattr(view, "refresh_filtered_results",
                            lambda: refreshes.append(view.search_query))

        view.search_field.value = "ph"
        view.on_search_change(None)

        assert refreshes == []  # not yet — the refresh is pending
        time.sleep(self.DELAY + 0.15)
        assert refreshes == ["ph"]

    def test_rapid_keystrokes_collapse_into_one_refresh(self, tmp_path, monkeypatch):
        view = self._view(tmp_path, monkeypatch)
        refreshes = []
        monkeypatch.setattr(view, "refresh_filtered_results",
                            lambda: refreshes.append(view.search_query))

        typed = ""
        for ch in "photo":
            typed += ch
            view.search_field.value = typed
            view.on_search_change(None)
            time.sleep(0.01)  # well under the debounce delay

        assert refreshes == []  # every keystroke reset the pending refresh
        time.sleep(self.DELAY + 0.15)
        assert refreshes == ["photo"]  # exactly one refresh, with the final query

    def test_callback_outside_scheduled_timer_is_ignored(self, tmp_path, monkeypatch):
        """The guard: only the timer that is CURRENTLY scheduled may perform
        the refresh — a late/superseded callback must stay silent (the newer
        timer owns the field)."""
        view = self._view(tmp_path, monkeypatch)
        refreshes = []
        monkeypatch.setattr(view, "refresh_filtered_results",
                            lambda: refreshes.append(view.search_query))
        view.SEARCH_DEBOUNCE_DELAY = 60.0  # armed, never fires within the test

        view.search_field.value = "old"
        view.on_search_change(None)

        # The armed timer's callback runs out-of-band (simulating the race
        # where a newer keystroke replaced the reference first).
        view._apply_search_query()
        assert refreshes == []

        view._search_debounce_timer.cancel()  # never fire the 60s timer


class TestLightboxProgressive:
    """2.1d: the lightbox used to ship the FULL-SIZE file to the client at
    open time. It must open on the cached thumbnail (a small JPEG, usually
    already generated for the result rows) and swap the full-size file in
    right after. A failed thumbnail falls back to the original path — the
    old behaviour is preserved."""

    class _FakePage:
        def __init__(self):
            self.dialogs = []

        def show_dialog(self, dlg):
            self.dialogs.append(dlg)

        def pop_dialog(self):
            pass

    def _view_with_page(self, tmp_path, monkeypatch):
        g = [make_info(str(tmp_path / "a0.bin"), 10),
             make_info(str(tmp_path / "a1.bin"), 10)]
        view = make_view({"k": g})
        fake_page = self._FakePage()
        monkeypatch.setattr(view, "_page_or_none", lambda: fake_page)
        return view, fake_page

    def test_opens_on_thumbnail_then_upgrades_to_full_size(self, tmp_path, monkeypatch):
        view, fake_page = self._view_with_page(tmp_path, monkeypatch)
        monkeypatch.setattr("ui.results_view.get_cached_thumbnail",
                            lambda p, mtime=None: "THUMB_CACHE/small.jpg")
        view.LIGHTBOX_FULL_IMAGE_DELAY = 0.05
        image_path = str(tmp_path / "photo.jpg")

        view.show_image_lightbox(image_path)

        (dlg,) = fake_page.dialogs
        assert dlg.content.content.src == "THUMB_CACHE/small.jpg"  # instant open
        time.sleep(0.2)
        assert dlg.content.content.src == image_path  # upgraded progressively

    def test_thumbnail_failure_falls_back_to_original(self, tmp_path, monkeypatch):
        """get_cached_thumbnail returns the original path when no preview can
        be made — the lightbox must open on it exactly like the old code."""
        view, fake_page = self._view_with_page(tmp_path, monkeypatch)
        monkeypatch.setattr("ui.results_view.get_cached_thumbnail",
                            lambda p, mtime=None: p)
        view.LIGHTBOX_FULL_IMAGE_DELAY = 60.0  # keep the upgrade out of the test
        image_path = str(tmp_path / "photo.jpg")

        view.show_image_lightbox(image_path)

        (dlg,) = fake_page.dialogs
        assert dlg.content.content.src == image_path
