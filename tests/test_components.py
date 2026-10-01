"""Tests for ui/components.py — pure logic: path formatting, themes, drives."""
import os
import re

import ui.components as components
from ui.components import (
    THEMES,
    format_path_short,
    get_active_theme_key,
    get_current_theme,
    get_detected_drives,
    get_styled_dialog,
    set_active_theme,
)

_HEX_COLOR = re.compile(r"^#[0-9A-Fa-f]{6}([0-9A-Fa-f]{2})?$")


class TestThemes:
    def test_default_theme_is_dark_slate(self):
        assert get_active_theme_key() == "dark_slate"
        assert get_current_theme() == THEMES["dark_slate"]

    def test_all_themes_share_identical_palette_keys(self):
        keys = {frozenset(t.keys()) for t in THEMES.values()}
        assert len(keys) == 1

    def test_theme_colors_are_valid_hex(self):
        for name, palette in THEMES.items():
            for key, value in palette.items():
                if key == "SHADOW_COLOR":
                    continue  # #RRGGBBAA is fine but documented separately
                assert _HEX_COLOR.match(value), f"{name}.{key}={value}"

    def test_set_active_theme_switches_palette(self):
        try:
            set_active_theme("light_clean")
            assert get_active_theme_key() == "light_clean"
            assert get_current_theme() == THEMES["light_clean"]
            assert components.PRIMARY_COLOR == THEMES["light_clean"]["PRIMARY_COLOR"]
        finally:
            set_active_theme("dark_slate")
        assert get_active_theme_key() == "dark_slate"
        assert components.PRIMARY_COLOR == THEMES["dark_slate"]["PRIMARY_COLOR"]

    def test_unknown_theme_name_is_ignored(self):
        before = get_active_theme_key()
        set_active_theme("does_not_exist")
        assert get_active_theme_key() == before

    def test_palette_aliases_match_theme(self):
        for key in components._PALETTE_KEYS:
            assert getattr(components, key) == THEMES[get_active_theme_key()][key]


class TestSetActiveThemeIsolation:
    """2.2c: set_active_theme used to scan ALL of sys.modules and rewrite any
    attribute whose VALUE matched the old palette — third-party modules that
    happened to hold the same string were silently corrupted. Only this
    module and the first-party palette consumers may be updated."""

    def test_foreign_modules_are_never_touched(self):
        import sys
        import types

        foreign = types.ModuleType("dup_theme_probe")
        dark = THEMES["dark_slate"]
        # Both a palette-key-named attribute and a differently-named one
        # hold old-palette strings: neither may be rewritten.
        foreign.PRIMARY_COLOR = dark["PRIMARY_COLOR"]
        foreign.MY_PAINT = dark["ACCENT_COLOR"]
        sys.modules["dup_theme_probe"] = foreign
        try:
            set_active_theme("light_clean")
            assert foreign.PRIMARY_COLOR == dark["PRIMARY_COLOR"]
            assert foreign.MY_PAINT == dark["ACCENT_COLOR"]
            # Switch twice — still no trace after returning to the start.
            set_active_theme("dark_slate")
            assert foreign.PRIMARY_COLOR == dark["PRIMARY_COLOR"]
            assert foreign.MY_PAINT == dark["ACCENT_COLOR"]
        finally:
            set_active_theme("dark_slate")
            del sys.modules["dup_theme_probe"]

    def test_own_module_and_consumers_update(self):
        import ui.search_view  # a real `from ui.components import PRIMARY_COLOR` consumer
        dark = THEMES["dark_slate"]
        light = THEMES["light_clean"]
        try:
            set_active_theme("light_clean")
            assert components.PRIMARY_COLOR == light["PRIMARY_COLOR"]
            assert ui.search_view.PRIMARY_COLOR == light["PRIMARY_COLOR"]
            assert get_current_theme() is light
        finally:
            set_active_theme("dark_slate")
        assert components.PRIMARY_COLOR == dark["PRIMARY_COLOR"]
        assert ui.search_view.PRIMARY_COLOR == dark["PRIMARY_COLOR"]


class TestFormatPathShort:
    def test_short_path_unchanged(self):
        path = os.path.join("C:", "Users", "me", "file.txt")
        assert format_path_short(path) == path

    def test_long_path_is_truncated_with_ellipsis(self):
        path = r"C:\Users\me\very\long\directory\structure\with\many\parts\file.txt"
        result = format_path_short(path, max_chars=40)
        assert "..." in result
        assert len(result) < len(path)
        assert result.startswith("C:\\")
        assert result.endswith("file.txt")

    def test_few_components_long_path_fallback(self):
        # Path with ≤2 components after the drive uses plain middle cut.
        path = "C:\\" + "x" * 200 + "\\named_file.dat"
        result = format_path_short(path, max_chars=40)
        assert "..." in result
        assert result.endswith("named_file.dat")

    def test_custom_max_chars(self):
        path = r"C:\a\b\c\d\e\f\g\h\i\j\k\file.txt"
        assert format_path_short(path, max_chars=120) == path


class TestGetDetectedDrives:
    def test_returns_only_existing_drives(self):
        drives = get_detected_drives()
        assert isinstance(drives, list)
        assert len(drives) >= 1  # at least the system drive exists
        for d in drives:
            assert d[1:] == ":\\"  # "X:\"
            assert os.path.exists(d)

    def test_system_drive_present(self):
        # Derived from the environment, not a hardcoded "C:\" — valid on any
        # Windows regardless of which drive hosts the OS.
        system_drive = os.environ.get("SystemDrive", "C:") + "\\"
        assert system_drive in get_detected_drives()


class TestStyledDialog:
    """Round 3 A7: two settings handlers ("Clear cache", "Context menu") called
    get_styled_dialog with no content and crashed with a TypeError AFTER the
    real work was done — content must be optional."""

    def test_dialog_without_content_does_not_crash(self):
        import flet as ft
        dlg = get_styled_dialog(title="Cache cleared", actions=[])
        assert isinstance(dlg, ft.AlertDialog)
        assert dlg.content is not None  # an empty container, never None

    def test_dialog_with_content_preserves_it(self):
        import flet as ft
        body = ft.Text("hello")
        dlg = get_styled_dialog(title="t", content=body)
        assert dlg.content is body

    def test_dialog_with_width_wraps_content(self):
        import flet as ft
        body = ft.Text("hello")
        dlg = get_styled_dialog(title="t", content=body, width=400)
        assert isinstance(dlg.content, ft.Container)
        assert dlg.content.content is body
