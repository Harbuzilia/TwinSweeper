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
