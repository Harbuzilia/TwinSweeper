"""Round-2 regression tests for the compare error path (main.run_compare +
CompareView.show_error). A failed comparison must surface as an error state —
the old code raised TypeError inside the error handler and then rendered the
failure as an empty "no common files" result."""

import flet as ft

from scanner import FileInfo
from ui.compare_view import CompareView


def make_compare_view(language="ru"):
    return CompareView(on_compare_start=lambda *a, **k: None, language=language)


class TestCompareErrorDisplay:
    def test_show_error_sets_error_state(self):
        view = make_compare_view()
        view.progress_bar.visible = True
        view.show_error("Ошибка сравнения: диск отвалился")
        assert view.progress_bar.visible is False
        assert "диск" in view.status_text.value
        assert view.status_text.color is not None  # error styling applied

    def test_show_error_is_safe_unmounted(self):
        # Constructed but never added to a page — must not raise, and must
        # still record the error state (not just "didn't crash").
        view = make_compare_view()
        view.show_error("any error")
        assert view.status_text.value == "any error"
        assert view.progress_bar.visible is False

    def test_start_compare_passes_error_callback(self, tmp_path):
        received = {}

        def fake_start(folder_a, folder_b, result_cb, progress_cb, error_cb=None):
            received["error_cb"] = error_cb

        view = CompareView(on_compare_start=fake_start, language="ru")
        view.folder_a = str(tmp_path)
        view.folder_b = str(tmp_path)
        view.start_compare(None)
        assert callable(received["error_cb"])
        # And it is the view's own error shower:
        received["error_cb"]("boom")
        assert "boom" in view.status_text.value
        assert view.progress_bar.visible is False


class TestTabReset:
    def test_show_results_resets_to_common_tab(self, tmp_path, monkeypatch):
        # The previous comparison's tab choice must not leak into a new one.
        view = make_compare_view()
        view.folder_a = str(tmp_path)
        view.folder_b = str(tmp_path)
        # flet's Control.parent is a read-only property; shadow it with a real
        # (unmounted) control so show_results does not take its early-return
        # while page resolution still fails the guarded way.
        monkeypatch.setattr(type(view), "parent", property(lambda self: ft.Container()))
        view.active_tab = "unique_b"

        view.show_results({"unique_a": [], "unique_b": [], "common": [], "total_files": 0})

        assert view.active_tab == "common"


class TestCompareBusyGuard:
    """Round 3: a double click on Compare used to launch two parallel
    compare_folders workers whose results interleaved."""

    def test_double_start_blocked_while_busy(self, tmp_path):
        calls = []

        def fake_start(a, b, result_cb, progress_cb, error_cb=None):
            calls.append((a, b))

        view = CompareView(on_compare_start=fake_start, language="ru")
        view.folder_a = str(tmp_path)
        view.folder_b = str(tmp_path)

        view.start_compare(None)
        view.start_compare(None)  # second click while the first is "running"

        assert len(calls) == 1  # only one worker launched
        assert view._compare_busy is True
        assert view.compare_button.visible is False

    def test_busy_cleared_after_results(self, tmp_path, monkeypatch):
        view = make_compare_view()
        view.folder_a = str(tmp_path)
        view.folder_b = str(tmp_path)
        monkeypatch.setattr(type(view), "parent", property(lambda self: ft.Container()))
        view._compare_busy = True
        view.compare_button.visible = False

        view.show_results({"unique_a": [], "unique_b": [], "common": [], "total_files": 0})

        assert view._compare_busy is False
        assert view.compare_button.visible is True

    def test_worker_launch_failure_resets_busy(self, tmp_path):
        """2.2e: if on_compare_start itself raises (the worker never
        launched), the view must fall back to the error state — the busy
        flag used to stay set with the Compare button hidden forever."""
        def exploding_start(a, b, result_cb, progress_cb, error_cb=None):
            raise RuntimeError("run_thread failed")

        view = CompareView(on_compare_start=exploding_start, language="ru")
        view.folder_a = str(tmp_path)
        view.folder_b = str(tmp_path)

        view.start_compare(None)  # must not raise

        assert view._compare_busy is False
        assert view.compare_button.visible is True
        assert view.progress_bar.visible is False
        assert "run_thread failed" in view.status_text.value


class TestResultsPagination:
    """2.1a: folder-compare results rendered one Control per file — a real
    folder tree (tens of thousands of files) froze the tab. The rows are now
    paged like results_view/sweeper_view (50 per page + "show N more")."""

    def _results(self, tmp_path, n_common, n_unique_a=0):
        def info(i):
            return FileInfo(str(tmp_path / f"f{i}.bin"), f"f{i}.bin", 10, 0.0, 0.0)

        common = [
            {
                "file_a": info(i), "file_b": info(i), "similarity": 1.0,
                "newer": "same", "larger": "same",
            }
            for i in range(n_common)
        ]
        unique_a = [info(1000 + i) for i in range(n_unique_a)]
        return {"unique_a": unique_a, "unique_b": [], "common": common,
                "total_files": n_common + n_unique_a}

    def _mounted_view(self, tmp_path, monkeypatch, results):
        view = make_compare_view()
        view.folder_a = str(tmp_path)
        view.folder_b = str(tmp_path)
        monkeypatch.setattr(type(view), "parent", property(lambda self: ft.Container()))
        monkeypatch.setattr(view, "update", lambda: None)
        view.show_results(results)
        return view

    def _rows(self, view):
        """Rows of the active tab card (get_styled_card -> Container -> Column)."""
        return view.results_container.controls[-1].content.controls

    def _file_row_count(self, view):
        # File rows are Containers; the load-more control is a Row wrapper.
        return sum(1 for r in self._rows(view) if isinstance(r, ft.Container))

    def _has_load_more(self, view):
        return any(isinstance(r, ft.Row) for r in self._rows(view))

    def test_first_page_rendered_then_grows(self, tmp_path, monkeypatch):
        view = self._mounted_view(tmp_path, monkeypatch, self._results(tmp_path, 120))

        assert view.loaded_items_count == view.ITEMS_PER_PAGE == 50
        assert self._file_row_count(view) == 50
        assert self._has_load_more(view) is True

        view.load_more_items(None)
        assert self._file_row_count(view) == 100
        assert self._has_load_more(view) is True

        view.load_more_items(None)
        assert self._file_row_count(view) == 120
        # Everything shown — the load-more control disappears.
        assert self._has_load_more(view) is False

    def test_small_result_has_no_load_more(self, tmp_path, monkeypatch):
        view = self._mounted_view(tmp_path, monkeypatch, self._results(tmp_path, 10))

        assert self._file_row_count(view) == 10
        assert self._has_load_more(view) is False

    def test_tab_switch_resets_pagination(self, tmp_path, monkeypatch):
        results = self._results(tmp_path, n_common=0, n_unique_a=120)
        view = self._mounted_view(tmp_path, monkeypatch, results)

        view.set_active_tab("unique_a")
        assert self._file_row_count(view) == 50

        view.load_more_items(None)
        assert self._file_row_count(view) == 100

        # Switching away and back starts the list over from the first page.
        view.set_active_tab("unique_b")
        view.set_active_tab("unique_a")
        assert self._file_row_count(view) == 50
        assert self._has_load_more(view) is True
