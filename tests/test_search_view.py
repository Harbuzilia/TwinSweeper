"""2.1e + 3.2: headless regression shield for ui/search_view.py.

Part 1 (2.1e, TestByteOnlyWarning): byte-only scans (by_byte without
by_hash) read the FULL content of every candidate file — the banner warns
about it, but the scan itself is NEVER blocked (non-blocking by design:
after M6 the reference is read once per group, still the whole data volume
passes through the disk).

Part 2 (3.2): the main scan screen as a contract — the form state must map
exactly onto on_scan_start kwargs, bad input (no folders / no criteria /
non-numeric or inverted sizes) must block the launch WITHOUT leaving the
buttons stuck in the mid-scan state, quick-exclude chips and drive chips
must edit the form, presets must set their criteria bundle, cancel must
invoke the worker's cancel fn, and a cancelled scan's late callbacks must
never overwrite a freshly started one (run-generation guard, round 3).

Headless: no page, all update() calls involved are guarded/patched —
same pattern as tests/test_sweeper_view.py.
"""

import flet as ft
import pytest

from locales import get_text
from ui.components import DANGER_COLOR
from ui.search_view import SearchView


def make_view(**kwargs):
    kwargs.setdefault("on_scan_start", lambda *a, **k: None)
    return SearchView(**kwargs)


def quiet(view, monkeypatch):
    """Neutralize update() calls that require a live flet page."""
    monkeypatch.setattr(view, "update", lambda: None)
    monkeypatch.setattr(view.exclude_field, "update", lambda: None)


def make_scannable(tmp_path, monkeypatch):
    """A view with one selected folder, recording every on_scan_start call."""
    started = []
    view = make_view(on_scan_start=lambda **kwargs: started.append(kwargs))
    quiet(view, monkeypatch)
    view.selected_directories = [str(tmp_path)]
    return view, started


def iter_controls(ctrl):
    """Depth-first walk of a flet control tree (Row/Column.controls + Container.content)."""
    yield ctrl
    for child in getattr(ctrl, "controls", None) or []:
        yield from iter_controls(child)
    content = getattr(ctrl, "content", None)
    if content is not None:
        yield from iter_controls(content)


def quick_chip(view, pattern):
    """Locate a quick-exclude chip by its '+ pattern' label."""
    for c in view.quick_chips_row.controls:
        if isinstance(c, ft.Container) and getattr(c.content, "value", None) == f"+ {pattern}":
            return c
    raise AssertionError(f"quick-exclude chip {pattern!r} not found")


class TestByteOnlyWarning:
    def test_warning_hidden_by_default(self):
        view = make_view()
        assert view.byte_only_warning.visible is False

    def test_warning_visible_when_byte_without_hash(self):
        view = make_view()
        view.check_byte.value = True
        view.check_hash.value = False
        view._update_byte_only_warning()
        assert view.byte_only_warning.visible is True

    def test_warning_hidden_when_byte_with_hash(self):
        """byte + hash is the paranoid combo: a full SHA-256 has already
        proven the content — no warning."""
        view = make_view()
        view.check_byte.value = True
        view.check_hash.value = True
        view._update_byte_only_warning()
        assert view.byte_only_warning.visible is False

    def test_warning_hidden_when_byte_off(self):
        view = make_view()
        view.check_byte.value = False
        view.check_hash.value = False
        view._update_byte_only_warning()
        assert view.byte_only_warning.visible is False

    def test_checkbox_handlers_keep_banner_in_sync(self):
        """The two checkboxes' on_change updates the banner live — the user
        sees the warning BEFORE pressing Start, not after."""
        view = make_view()
        view.check_byte.value = True
        view.check_hash.value = False
        view.check_byte.on_change(None)
        assert view.byte_only_warning.visible is True

        view.check_hash.value = True
        view.check_hash.on_change(None)
        assert view.byte_only_warning.visible is False

    def test_banner_is_part_of_the_layout(self):
        """Top-level banner (next to the progress card): visible even when
        the Advanced section is collapsed."""
        view = make_view()
        assert view.byte_only_warning in view.controls

    def test_presets_keep_banner_hidden(self, monkeypatch):
        """No preset produces byte-without-hash (paranoid = hash+byte), so
        preset selection must never light the banner."""
        view = make_view()
        monkeypatch.setattr(view, "update", lambda: None)
        for preset in ("turbo", "standard", "paranoid", "phash"):
            view.select_preset(preset)
            assert view.byte_only_warning.visible is False, preset

    def test_warning_does_not_block_scan_start(self, tmp_path, monkeypatch):
        """Non-blocking contract: a byte-only scan STARTS despite the
        warning, and the banner is lit while it runs."""
        started = {}

        def fake_start(**kwargs):
            started.update(kwargs)

        view = SearchView(on_scan_start=fake_start, language="ru")
        monkeypatch.setattr(view, "update", lambda: None)
        view.selected_directories = [str(tmp_path)]
        view.check_byte.value = True
        view.check_hash.value = False

        view.start_scan(None)

        assert started.get("by_byte") is True
        assert started.get("by_hash") is False
        assert view.byte_only_warning.visible is True


class TestScanStartValidation:
    """start_scan must refuse to launch on bad input — and must not leave
    the buttons stuck in the mid-scan state while refusing."""

    def test_start_without_folders_blocks_scan(self, monkeypatch):
        started = []
        view = make_view(on_scan_start=lambda **kwargs: started.append(kwargs))
        quiet(view, monkeypatch)
        view.selected_directories = []

        view.start_scan(None)

        assert started == []
        assert view.progress_text.value == get_text("please_select_dir", "ru")
        assert view.status_icon.visible is True
        assert view.status_icon.icon == ft.Icons.ERROR_OUTLINE_ROUNDED
        # The error is informational — Start stays available.
        assert view.start_button.visible is True

    def test_no_criteria_blocks_scan(self, tmp_path, monkeypatch):
        """All criteria off (non-phash) would treat EVERY file as one
        duplicate group pre-selected for deletion — refused."""
        view, started = make_scannable(tmp_path, monkeypatch)
        for box in (view.check_size, view.check_hash, view.check_name, view.check_byte):
            box.value = False

        view.start_scan(None)

        assert started == []
        assert view.progress_text.value == get_text("error_no_criteria", "ru")
        assert view.start_button.visible is True

    def test_nonnumeric_min_blocks_scan(self, tmp_path, monkeypatch):
        """An invalid min-size must be flagged, not silently treated as 0
        (which quietly disabled the filter)."""
        view, started = make_scannable(tmp_path, monkeypatch)
        view.min_size_field.value = "abc"

        view.start_scan(None)

        assert started == []
        assert view.min_size_field.error_text == get_text("error_invalid_number", "ru")
        assert view.min_size_field.border_color == DANGER_COLOR
        # Validation happens BEFORE the button flip — no stuck "scanning".
        assert view.start_button.visible is True
        assert view.cancel_button.visible is False
        assert view.progress_bar.visible is False

    def test_nonnumeric_max_blocks_scan(self, tmp_path, monkeypatch):
        view, started = make_scannable(tmp_path, monkeypatch)
        view.max_size_field.value = "12x"

        view.start_scan(None)

        assert started == []
        assert view.max_size_field.error_text == get_text("error_invalid_number", "ru")
        assert view.max_size_field.border_color == DANGER_COLOR
        assert view.start_button.visible is True

    def test_max_below_min_blocks_scan(self, tmp_path, monkeypatch):
        """min > max would silently scan nothing — the form must say so."""
        view, started = make_scannable(tmp_path, monkeypatch)
        view.min_size_field.value = "100"
        view.max_size_field.value = "5"

        view.start_scan(None)

        assert started == []
        assert view.max_size_field.error_text == get_text("error_max_below_min", "ru")
        assert view.max_size_field.border_color == DANGER_COLOR
        assert view.start_button.visible is True

    def test_size_validation_error_clears_on_next_valid_start(self, tmp_path, monkeypatch):
        """A fixed value must clear the previous error flag — no stale red
        border left over a successfully started scan."""
        view, started = make_scannable(tmp_path, monkeypatch)
        view.min_size_field.value = "abc"
        view.start_scan(None)
        assert view.min_size_field.error_text is not None

        view.min_size_field.value = "5"
        view.start_scan(None)

        assert len(started) == 1
        assert view.min_size_field.error_text is None


class TestScanStartParamMapping:
    """The form is the only source of scan parameters — every control must
    land in the on_scan_start kwargs with the right conversion."""

    def test_start_scan_maps_form_to_params(self, tmp_path, monkeypatch):
        view, started = make_scannable(tmp_path, monkeypatch)
        view.check_name.value = True
        view.check_size.value = False
        view.check_turbo.value = False
        view.check_ignore_empty.value = False
        view.min_size_field.value = "5"
        view.max_size_field.value = "100"
        view.exclude_field.value = ".git, node_modules , , *.log"

        view.start_scan(None)

        assert len(started) == 1
        params = started[0]
        assert params["directories"] == [str(tmp_path)]
        assert params["by_name"] is True
        assert params["by_size"] is False
        assert params["by_hash"] is True  # untouched checkbox keeps its value
        assert params["by_byte"] is False
        assert params["turbo_mode"] is False
        assert params["min_size_bytes"] == 5 * 1024
        assert params["max_size_bytes"] == 100 * 1024
        assert params["exclude_patterns"] == [".git", "node_modules", "*.log"]
        assert params["ignore_empty_files"] is False
        assert params["is_phash"] is False
        assert params["phash_threshold"] == pytest.approx(0.90)
        # The scan machinery kwargs are wired (guarded callbacks).
        assert callable(params["progress_callback"])
        assert callable(params["on_cancel_setup"])
        assert callable(params["on_scan_finished"])

    def test_start_scan_flips_buttons_to_scanning_state(self, tmp_path, monkeypatch):
        view, _ = make_scannable(tmp_path, monkeypatch)

        view.start_scan(None)

        assert view.start_button.visible is False
        assert view.cancel_button.visible is True
        assert view.progress_bar.visible is True
        assert view.progress_bar.value is None  # indeterminate while scanning
        assert view.progress_text.value == get_text("scanning", "ru")
        assert view.status_icon.visible is True

    @pytest.mark.parametrize("max_value", ["0", "", "-5"])
    def test_nonpositive_max_means_no_upper_limit(self, max_value, tmp_path, monkeypatch):
        """0 / empty / negative max is the documented 'no upper limit'."""
        view, started = make_scannable(tmp_path, monkeypatch)
        view.max_size_field.value = max_value

        view.start_scan(None)

        assert started[0]["max_size_bytes"] is None

    def test_empty_min_size_defaults_to_zero(self, tmp_path, monkeypatch):
        view, started = make_scannable(tmp_path, monkeypatch)
        view.min_size_field.value = ""

        view.start_scan(None)

        assert started[0]["min_size_bytes"] == 0

    def test_equal_min_max_is_not_an_error(self, tmp_path, monkeypatch):
        """min == max is a valid single-size window; only strictly-below is."""
        view, started = make_scannable(tmp_path, monkeypatch)
        view.min_size_field.value = "5"
        view.max_size_field.value = "5"

        view.start_scan(None)

        assert len(started) == 1
        assert started[0]["max_size_bytes"] == 5 * 1024
        assert view.max_size_field.error_text is None


class TestPresets:
    """Preset tiles are one-click criteria bundles — the mapping is a
    user-facing contract (the badge text on each tile promises it)."""

    @pytest.mark.parametrize(
        "preset, size, hash_, name, byte, turbo",
        [
            ("turbo", True, True, False, False, True),
            ("standard", True, True, False, False, False),
            ("paranoid", True, True, False, True, False),
            ("phash", False, True, False, False, True),
        ],
    )
    def test_preset_sets_expected_criteria(self, preset, size, hash_, name, byte, turbo):
        view = make_view()
        view.select_preset(preset)
        assert view.active_preset == preset
        assert view.check_size.value is size
        assert view.check_hash.value is hash_
        assert view.check_name.value is name
        assert view.check_byte.value is byte
        assert view.check_turbo.value is turbo

    def test_phash_options_only_visible_in_phash_mode(self):
        view = make_view()
        for preset in ("turbo", "standard", "paranoid"):
            view.select_preset(preset)
            assert view.phash_options_container.visible is False, preset
        view.select_preset("phash")
        assert view.phash_options_container.visible is True

    def test_phash_scan_passes_slider_threshold_and_bypasses_criteria(self, tmp_path, monkeypatch):
        """pHash matches images by content, not by these checkboxes — the
        criteria guard is skipped and the slider drives phash_threshold."""
        view, started = make_scannable(tmp_path, monkeypatch)
        view.select_preset("phash")
        view.similarity_slider.value = 80
        for box in (view.check_size, view.check_hash, view.check_name, view.check_byte):
            box.value = False

        view.start_scan(None)

        assert len(started) == 1
        assert started[0]["is_phash"] is True
        assert started[0]["phash_threshold"] == pytest.approx(0.8)


class TestQuickExcludeChips:
    """Quick-exclude chips append patterns onto the exclude field —
    no duplicates, manually typed input preserved and cleaned up."""

    def test_chip_click_appends_pattern(self, monkeypatch):
        view = make_view()
        quiet(view, monkeypatch)

        quick_chip(view, ".git").on_click(None)

        assert view.exclude_field.value == ".git"

    def test_chip_click_ignores_duplicates(self, monkeypatch):
        view = make_view()
        quiet(view, monkeypatch)

        quick_chip(view, "node_modules").on_click(None)
        quick_chip(view, "node_modules").on_click(None)

        assert view.exclude_field.value == "node_modules"

    def test_chip_click_preserves_and_strips_existing_patterns(self, monkeypatch):
        """Existing manual input survives, whitespace-only entries drop."""
        view = make_view()
        quiet(view, monkeypatch)
        view.exclude_field.value = " my_dir ,  "

        quick_chip(view, "*.log").on_click(None)

        assert view.exclude_field.value == "my_dir, *.log"


class TestFolderManagement:
    """add_drive / remove_folder / clear_all manage the scan target list
    and must stay in sync with the rendered folder list."""

    def test_add_drive_appends_and_renders_folder_item(self):
        view = make_view()
        view.add_drive("Q:\\")
        assert view.selected_directories == ["Q:\\"]
        assert len(view.folders_container.controls) == 1

    def test_add_drive_ignores_duplicates(self):
        view = make_view()
        view.add_drive("Q:\\")
        view.add_drive("Q:\\")
        assert view.selected_directories == ["Q:\\"]
        assert len(view.folders_container.controls) == 1

    def test_folder_item_remove_button_removes_folder(self):
        """The X button rendered inside the folder list item actually
        removes the folder (on_remove wiring)."""
        view = make_view()
        view.add_drive("Q:\\")
        item = view.folders_container.controls[0]
        remove_btn = next(
            c for c in iter_controls(item)
            if isinstance(c, ft.Container) and c.on_click is not None
        )

        remove_btn.on_click(None)

        assert view.selected_directories == []
        assert view.folders_container.controls == []

    def test_clear_all_folders_empties_selection(self):
        view = make_view()
        view.add_drive("Q:\\")
        view.add_drive("Z:\\")

        view.clear_all_folders(None)

        assert view.selected_directories == []
        assert view.folders_container.controls == []

    def test_drive_chip_click_adds_drive(self, monkeypatch):
        """The drive chips rendered by build_ui must be wired to add_drive."""
        view = make_view()
        monkeypatch.setattr(view, "get_detected_drives", lambda: ["Q:\\", "Z:\\"])
        view.build_ui()
        chip = next(
            c for c in iter_controls(view)
            if isinstance(c, ft.Container) and c.tooltip == "Q:\\"
        )

        chip.on_click(None)

        assert view.selected_directories == ["Q:\\"]


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
        view.selected_directories = [str(tmp_path)]
        view.start_scan(None)

        view.on_cancel_click(None)

        assert called == ["cancelled"]
        assert view.progress_text.value == get_text("scan_cancelled", "ru")
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
        assert view.progress_text.value == get_text("scan_cancelled", "ru")

    def test_late_cancelled_scan_callbacks_do_not_touch_new_scan(self, tmp_path, monkeypatch):
        """Round 3: cancel + immediately start a new scan — the OLD worker's
        late callbacks must not overwrite the new scan's UI (a stale
        'cancelled' message used to reset the buttons mid-scan and allowed a
        third parallel scan)."""
        started = []
        view = make_view(on_scan_start=lambda **kwargs: started.append(kwargs))
        quiet(view, monkeypatch)
        view.selected_directories = [str(tmp_path)]

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
        assert view.progress_text.value == get_text("scanning", "ru")
        assert view.progress_bar.value is None
        assert view.start_button.visible is False
        assert view.cancel_button.visible is True

        # Scan #2's own callbacks still work normally.
        new["progress_callback"]("fresh status", 0.5)
        assert view.progress_text.value == "fresh status"
        new["on_scan_finished"]()
        assert view.start_button.visible is True
        assert view.cancel_button.visible is False


class TestStatusAndReset:
    """update_status / reset_state keep the progress card honest."""

    def test_update_status_clamps_percent(self):
        """percent is a 0..1 fraction (scanner.py reports 0.05/0.9/1.0) —
        out-of-range worker values must not break the bar."""
        view = make_view()

        view.update_status("halfway", 0.5)
        assert view.progress_text.value == "halfway"
        assert view.progress_bar.value == 0.5

        view.update_status("over", 1.5)
        assert view.progress_bar.value == 1.0

        view.update_status("under", -0.1)
        assert view.progress_bar.value == 0.0

        view.update_status("indeterminate")
        assert view.progress_bar.value is None

    def test_reset_state_restores_idle_ui(self, monkeypatch):
        view = make_view()
        quiet(view, monkeypatch)
        # Simulate mid-scan leftovers.
        view.start_button.visible = False
        view.cancel_button.visible = True
        view.progress_bar.visible = True
        view.progress_text.value = "scanning..."
        view.status_icon.visible = True

        view.reset_state()

        assert view.start_button.visible is True
        assert view.cancel_button.visible is False
        assert view.progress_bar.visible is False
        assert view.progress_text.value == ""
        assert view.status_icon.visible is False
