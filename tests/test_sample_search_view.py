"""3.3: headless regression shield for ui/sample_search_view.py.

The "search by sample file" screen as a contract: picking a sample must land
its name/size/path on the sample card, a sample that vanished between pick
and render must not crash (silent no-op) nor hide behind a missing error
branch (the stat-race shows error_no_sample), start_scan must refuse to
launch without a sample / folders / criteria WITHOUT leaving the buttons
stuck mid-scan, the criteria checkboxes must map exactly onto the
on_scan_start kwargs, and a cancelled scan's late callbacks must never
overwrite a freshly started one (run-generation guard, same as search_view).

README invariant "similar is NOT preselected for deletion": the screen's
only output is the scan contract — it carries no selection/delete flag and
keeps by_hash/by_byte honest, the flags main.py turns into content_verified
(ResultsView preselects only content-verified groups).

Headless: no page, all update() calls involved are patched via quiet() —
same pattern as tests/test_search_view.py.
"""

import asyncio
import datetime
import os

import flet as ft

from locales import get_text
from scanner import format_file_size, scan_for_sample
from ui.components import DANGER_COLOR, TEXT_SECONDARY, format_path_short
from ui.sample_search_view import SampleSearchView


def make_view(**kwargs):
    kwargs.setdefault("on_scan_start", lambda *a, **k: None)
    return SampleSearchView(**kwargs)


def quiet(view, monkeypatch):
    """Neutralize update() calls that require a live flet page."""
    monkeypatch.setattr(view, "update", lambda: None)


def make_scannable(tmp_path, monkeypatch):
    """A view with a real sample file and one selected folder, recording
    every on_scan_start call."""
    started = []
    sample = tmp_path / "sample.bin"
    sample.write_bytes(b"S" * 2048)
    view = make_view(on_scan_start=lambda **kwargs: started.append(kwargs))
    quiet(view, monkeypatch)
    view.sample_path = str(sample)
    view.search_directories = [str(tmp_path)]
    return view, started, sample


def iter_controls(ctrl):
    """Depth-first walk of a flet control tree (Row/Column.controls + Container.content)."""
    yield ctrl
    for child in getattr(ctrl, "controls", None) or []:
        yield from iter_controls(child)
    content = getattr(ctrl, "content", None)
    if content is not None:
        yield from iter_controls(content)


def texts_in(ctrl):
    """Every ft.Text value rendered inside a control subtree."""
    return [c.value for c in iter_controls(ctrl) if isinstance(c, ft.Text)]


class _FakePickedFile:
    """Minimal stand-in for flet FilePicker's returned file object."""

    def __init__(self, path):
        self.path = path


class TestSampleSelection:
    """pick_sample / update_sample_card: the chosen sample must actually
    appear on the card, and a vanished sample must not crash the render."""

    def test_pick_sample_sets_path_and_renders_name(self, tmp_path, monkeypatch):
        # Arrange: a real file and a FilePicker dialog "returning" it.
        view = make_view()
        quiet(view, monkeypatch)
        sample = tmp_path / "report.pdf"
        sample.write_bytes(b"pdf-bytes")

        async def fake_pick_files(allow_multiple=False):
            return [_FakePickedFile(str(sample))]

        monkeypatch.setattr(view.pick_sample_dialog, "pick_files", fake_pick_files)

        # Act
        asyncio.run(view.pick_sample(None))

        # Assert
        assert view.sample_path == str(sample)
        assert "report.pdf" in texts_in(view.sample_info_container)

    def test_pick_sample_with_empty_dialog_keeps_state(self, monkeypatch):
        """A cancelled picker dialog must leave the view untouched."""
        view = make_view()
        quiet(view, monkeypatch)

        async def fake_pick_files(allow_multiple=False):
            return None

        monkeypatch.setattr(view.pick_sample_dialog, "pick_files", fake_pick_files)

        asyncio.run(view.pick_sample(None))

        assert view.sample_path is None
        assert get_text("no_sample_selected", "ru") in texts_in(view.sample_info_container)

    def test_update_sample_card_shows_name_size_and_short_path(self, tmp_path, monkeypatch):
        """The card must render the basename, human size, short path and
        modification date of the chosen sample."""
        view, _, sample = make_scannable(tmp_path, monkeypatch)

        view.update_sample_card()

        texts = texts_in(view.sample_info_container)
        assert sample.name in texts
        assert format_file_size(sample.stat().st_size) in texts  # "2.00 KB"
        assert format_path_short(str(sample)) in texts
        expected_date = datetime.datetime.fromtimestamp(sample.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
        assert expected_date in texts

    def test_update_sample_card_without_sample_keeps_hint(self, monkeypatch):
        view = make_view()
        quiet(view, monkeypatch)
        view.sample_path = None

        view.update_sample_card()

        assert get_text("no_sample_selected", "ru") in texts_in(view.sample_info_container)

    def test_update_sample_card_with_deleted_sample_is_noop(self, tmp_path, monkeypatch):
        """Sample deleted between pick and render: the card must keep its
        previous render (no crash, no broken half-rendered state)."""
        view, _, sample = make_scannable(tmp_path, monkeypatch)
        view.update_sample_card()  # render the real card first
        before = view.sample_info_container
        sample.unlink()  # the sample vanishes

        view.update_sample_card()

        assert view.sample_info_container is before
        assert sample.name in texts_in(before)  # previous render preserved

    def test_update_sample_card_stat_race_shows_error(self, tmp_path, monkeypatch):
        """exists() said yes, stat() says no (unplugged drive / permission
        race): the user must see error_no_sample instead of a crash."""
        view, _, sample = make_scannable(tmp_path, monkeypatch)
        monkeypatch.setattr(os.path, "exists", lambda p: True)

        def broken_stat(p):
            raise OSError("drive unplugged")

        monkeypatch.setattr(os, "stat", broken_stat)

        view.update_sample_card()

        assert view.status_text.value == get_text("error_no_sample", "ru")
        assert view.status_text.color == DANGER_COLOR


class TestScanStartValidation:
    """start_scan must refuse to launch on bad input — and must not leave
    the buttons stuck in the mid-scan state while refusing."""

    def test_start_without_sample_blocks_scan(self, tmp_path, monkeypatch):
        view, started, _ = make_scannable(tmp_path, monkeypatch)
        view.sample_path = None

        view.start_scan(None)

        assert started == []
        assert view.status_text.value == get_text("error_no_sample", "ru")
        assert view.status_text.color == DANGER_COLOR
        assert view.status_icon.visible is True
        assert view.status_icon.icon == ft.Icons.ERROR_OUTLINE_ROUNDED
        assert view.status_icon.color == DANGER_COLOR
        # The error is informational — Start stays available.
        assert view.start_button.visible is True
        assert view.cancel_button.visible is False
        assert view.progress_bar.visible is False

    def test_start_without_folders_blocks_scan(self, tmp_path, monkeypatch):
        view, started, sample = make_scannable(tmp_path, monkeypatch)
        view.search_directories = []

        view.start_scan(None)

        assert started == []
        assert view.status_text.value == get_text("error_no_scope", "ru")
        assert view.start_button.visible is True
        assert view.cancel_button.visible is False
        assert view.progress_bar.visible is False

    def test_start_without_criteria_blocks_scan(self, tmp_path, monkeypatch):
        """All criteria off would report every walked file as a copy of the
        sample — refused."""
        view, started, _ = make_scannable(tmp_path, monkeypatch)
        for box in (view.check_size, view.check_hash, view.check_name, view.check_byte):
            box.value = False

        view.start_scan(None)

        assert started == []
        assert view.status_text.value == get_text("error_no_criteria", "ru")
        assert view.start_button.visible is True
        assert view.cancel_button.visible is False
        assert view.progress_bar.visible is False

    def test_start_scan_flips_buttons_to_scanning_state(self, tmp_path, monkeypatch):
        view, started, _ = make_scannable(tmp_path, monkeypatch)

        view.start_scan(None)

        assert len(started) == 1
        assert view.start_button.visible is False
        assert view.cancel_button.visible is True
        assert view.progress_bar.visible is True
        assert view.progress_bar.value is None  # indeterminate while scanning
        assert view.status_text.value == get_text("scanning", "ru")
        assert view.status_text.color == TEXT_SECONDARY
        assert view.status_icon.visible is True


class TestCriteriaMapping:
    """The criteria checkboxes are the only source of scan parameters —
    every control must land in the on_scan_start kwargs unchanged."""

    def test_start_scan_maps_criteria_to_params(self, tmp_path, monkeypatch):
        view, started, sample = make_scannable(tmp_path, monkeypatch)
        view.check_name.value = True
        view.check_size.value = False
        view.check_hash.value = False
        view.check_byte.value = True

        view.start_scan(None)

        assert len(started) == 1
        params = started[0]
        assert params["sample_path"] == str(sample)
        assert params["directories"] == [str(tmp_path)]
        assert params["by_name"] is True
        assert params["by_size"] is False
        assert params["by_hash"] is False
        assert params["by_byte"] is True
        # The scan machinery kwargs are wired (guarded callbacks).
        assert callable(params["progress_callback"])
        assert callable(params["on_cancel_setup"])
        assert callable(params["on_scan_finished"])

    def test_default_criteria_map_size_and_hash(self, tmp_path, monkeypatch):
        """Untouched checkboxes keep their defaults (size+hash on)."""
        view, started, _ = make_scannable(tmp_path, monkeypatch)

        view.start_scan(None)

        params = started[0]
        assert params["by_size"] is True
        assert params["by_hash"] is True
        assert params["by_name"] is False
        assert params["by_byte"] is False

    def test_scan_contract_carries_no_deletion_preselection(self, tmp_path, monkeypatch):
        """README invariant: similar matches are NOT preselected for
        deletion. The screen's only output is the scan contract — no
        selection/delete flag of any kind — and for a name-only scan
        (content never compared) by_hash/by_byte stay False: the exact
        flags main.py turns into content_verified, on which ResultsView
        gates its delete-preselection."""
        view, started, _ = make_scannable(tmp_path, monkeypatch)
        view.check_name.value = True
        view.check_size.value = False
        view.check_hash.value = False
        view.check_byte.value = False

        view.start_scan(None)

        params = started[0]
        assert set(params) == {
            "sample_path", "directories",
            "by_name", "by_size", "by_hash", "by_byte",
            "progress_callback", "on_cancel_setup", "on_scan_finished",
        }
        assert params["by_hash"] is False
        assert params["by_byte"] is False
        # The screen holds no deletion-selection state of its own to leak.
        assert not hasattr(view, "selected_paths")


class TestFolderManagement:
    """add_drive / remove_folder / pick_folder manage the search target
    list and must stay in sync with the rendered folder list."""

    def test_add_drive_appends_and_renders_folder_item(self, monkeypatch):
        view = make_view()
        quiet(view, monkeypatch)

        view.add_drive("Q:\\")

        assert view.search_directories == ["Q:\\"]
        assert len(view.folders_container.controls) == 1

    def test_add_drive_ignores_duplicates(self, monkeypatch):
        view = make_view()
        quiet(view, monkeypatch)

        view.add_drive("Q:\\")
        view.add_drive("Q:\\")

        assert view.search_directories == ["Q:\\"]
        assert len(view.folders_container.controls) == 1

    def test_remove_folder_removes_from_selection(self, monkeypatch):
        view = make_view()
        quiet(view, monkeypatch)
        view.add_drive("Q:\\")
        view.add_drive("Z:\\")

        view.remove_folder("Q:\\")

        assert view.search_directories == ["Z:\\"]
        assert len(view.folders_container.controls) == 1

    def test_folder_item_remove_button_removes_folder(self, monkeypatch):
        """The X button rendered inside the folder list item actually
        removes the folder (on_remove wiring)."""
        view = make_view()
        quiet(view, monkeypatch)
        view.add_drive("Q:\\")
        item = view.folders_container.controls[0]
        remove_btn = next(
            c for c in iter_controls(item)
            if isinstance(c, ft.Container) and c.on_click is not None
        )

        remove_btn.on_click(None)

        assert view.search_directories == []
        assert view.folders_container.controls == []

    def test_drive_chip_click_adds_drive(self, monkeypatch):
        """The drive chips rendered by build_ui must be wired to add_drive."""
        view = make_view()
        quiet(view, monkeypatch)
        monkeypatch.setattr(view, "get_detected_drives", lambda: ["Q:\\", "Z:\\"])
        view.build_ui()
        chip = next(
            c for c in iter_controls(view)
            if isinstance(c, ft.Container) and c.tooltip == "Q:\\"
        )

        chip.on_click(None)

        assert view.search_directories == ["Q:\\"]

    def test_pick_folder_appends_folder_and_ignores_duplicate(self, tmp_path, monkeypatch):
        view = make_view()
        quiet(view, monkeypatch)

        async def fake_get_directory_path():
            return str(tmp_path)

        monkeypatch.setattr(view.pick_folders_dialog, "get_directory_path", fake_get_directory_path)

        asyncio.run(view.pick_folder(None))
        asyncio.run(view.pick_folder(None))  # same folder picked again

        assert view.search_directories == [str(tmp_path)]
        assert len(view.folders_container.controls) == 1


class TestCancelAndRunGenerationGuard:
    """Cancel must invoke the worker-registered cancel fn and restore the
    idle buttons; the run-generation guard keeps a cancelled scan's late
    callbacks away from a freshly started scan (round 3)."""

    def test_on_cancel_click_invokes_registered_cancel_fn(self, tmp_path, monkeypatch):
        """on_cancel_setup hands the view a cancel fn — clicking Cancel must
        call it (the worker stops) and reset the action buttons."""
        called = []

        def fake_start(**kwargs):
            kwargs["on_cancel_setup"](lambda: called.append("cancelled"))

        view = make_view(on_scan_start=fake_start)
        quiet(view, monkeypatch)
        view.sample_path = str(tmp_path / "sample.bin")
        (tmp_path / "sample.bin").write_bytes(b"S" * 16)
        view.search_directories = [str(tmp_path)]
        view.start_scan(None)

        view.on_cancel_click(None)

        assert called == ["cancelled"]
        assert view.status_text.value == get_text("scan_cancelled", "ru")
        assert view.start_button.visible is True
        assert view.cancel_button.visible is False
        assert view.progress_bar.visible is False
        assert view.status_icon.visible is False

    def test_on_cancel_click_without_callback_is_safe(self, monkeypatch):
        """An idle view (no scan, no registered callback): Cancel must not
        crash and must still restore the buttons."""
        view = make_view()
        quiet(view, monkeypatch)
        view.cancel_callback = None

        view.on_cancel_click(None)

        assert view.start_button.visible is True
        assert view.status_text.value == get_text("scan_cancelled", "ru")

    def test_late_cancelled_scan_callbacks_do_not_touch_new_scan(self, tmp_path, monkeypatch):
        """Round 3: cancel + immediately start a new scan — the OLD worker's
        late callbacks must not overwrite the new scan's UI (a stale
        'cancelled' message used to reset the buttons mid-scan)."""
        started = []
        view = make_view(on_scan_start=lambda **kwargs: started.append(kwargs))
        quiet(view, monkeypatch)
        view.sample_path = str(tmp_path / "sample.bin")
        (tmp_path / "sample.bin").write_bytes(b"S" * 16)
        view.search_directories = [str(tmp_path)]

        view.start_scan(None)  # scan #1
        old = started[0]
        view.on_cancel_click(None)  # user cancels scan #1

        view.start_scan(None)  # user immediately starts scan #2
        new = started[1]
        assert view.start_button.visible is False  # scan #2 is running
        assert view.cancel_button.visible is True

        # The cancelled worker delivers its late callbacks...
        old["progress_callback"]("stale status", 0.1)
        old["on_scan_finished"]()
        # ...the guard must keep them away from scan #2's UI.
        assert view.status_text.value == get_text("scanning", "ru")
        assert view.progress_bar.value is None
        assert view.start_button.visible is False
        assert view.cancel_button.visible is True

        # Scan #2's own callbacks still work normally.
        new["progress_callback"]("fresh status", 0.5)
        assert view.status_text.value == "fresh status"
        new["on_scan_finished"]()
        assert view.start_button.visible is True
        assert view.cancel_button.visible is False


class TestStatusAndResultsDisplay:
    """update_status / on_scan_finished keep the progress card honest while
    the worker reports progress and results."""

    def test_update_status_clamps_percent(self):
        """percent is a 0..1 fraction — out-of-range worker values must not
        break the bar."""
        view = make_view()

        view.update_status("halfway", 0.5)
        assert view.status_text.value == "halfway"
        assert view.progress_bar.value == 0.5

        view.update_status("over", 1.5)
        assert view.progress_bar.value == 1.0

        view.update_status("under", -0.1)
        assert view.progress_bar.value == 0.0

        view.update_status("indeterminate")
        assert view.progress_bar.value is None

    def test_progress_callback_renders_worker_messages(self, tmp_path, monkeypatch):
        """The guarded progress_callback handed to the worker must land its
        message and percent on the progress card."""
        view, started, _ = make_scannable(tmp_path, monkeypatch)
        view.start_scan(None)
        callback = started[0]["progress_callback"]

        callback("checked 100 files", 0.4)

        assert view.status_text.value == "checked 100 files"
        assert view.progress_bar.value == 0.4

        callback("wrapping up", None)
        assert view.progress_bar.value is None

    def test_finished_callback_restores_idle_buttons(self, tmp_path, monkeypatch):
        view, started, _ = make_scannable(tmp_path, monkeypatch)
        view.start_scan(None)

        started[0]["on_scan_finished"]()

        assert view.start_button.visible is True
        assert view.cancel_button.visible is False
        assert view.progress_bar.visible is False
        assert view.status_icon.visible is False

    def test_vanished_sample_scan_reports_no_duplicates_end_to_end(
        self, tmp_path, monkeypatch, fresh_cache_db
    ):
        """Sample deleted between pick and start (actual contract): the
        screen delegates to the scan machinery, whose exists-guard in
        scan_for_sample returns no copies; the flow ends with the honest
        'no duplicates' status and restored buttons — no crash, no scan
        output left behind."""
        target = tmp_path / "target"
        target.mkdir()

        def fake_run_sample_scan(**kwargs):
            found = scan_for_sample(
                sample_path=kwargs["sample_path"],
                search_directories=kwargs["directories"],
                by_name=kwargs["by_name"],
                by_size=kwargs["by_size"],
                by_hash=kwargs["by_hash"],
                by_byte=kwargs["by_byte"],
            )
            if not found:
                kwargs["progress_callback"](get_text("no_duplicates", "ru"), None)
            kwargs["on_scan_finished"]()

        view = make_view(on_scan_start=fake_run_sample_scan)
        quiet(view, monkeypatch)
        sample = tmp_path / "gone.bin"
        sample.write_bytes(b"payload")
        view.sample_path = str(sample)
        view.search_directories = [str(target)]
        sample.unlink()  # vanished between pick and start

        view.start_scan(None)

        assert view.status_text.value == get_text("no_duplicates", "ru")
        assert view.start_button.visible is True
        assert view.cancel_button.visible is False
        assert view.progress_bar.visible is False
