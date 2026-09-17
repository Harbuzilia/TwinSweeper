"""Round-2 regression tests for the compare error path (main.run_compare +
CompareView.show_error). A failed comparison must surface as an error state —
the old code raised TypeError inside the error handler and then rendered the
failure as an empty "no common files" result."""

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
        # Constructed but never added to a page — must not raise.
        view = make_compare_view()
        view.show_error("any error")

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
