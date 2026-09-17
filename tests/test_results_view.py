"""Regression tests for the Этап-2 results_view.py fixes — headless (no page).

Covers C3 (original protection on "select all"/"invert"), M9 (wasted-space
sums actual duplicate sizes, not (n-1) * first file), C1a (hardlink button
hidden for visually-similar results) and C2's UI side (selected entries carry
size/mtime snapshots). The view is constructed without a page: every
self.update() in the touched code paths is guarded.

Data-safety: view models only — no real files are touched at all.
"""
import os

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
