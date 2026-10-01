"""2.1e: byte-only scans (by_byte without by_hash) read the FULL content of
every candidate file — the banner warns about it, but the scan itself is
NEVER blocked (non-blocking by design: after M6 the reference is read once
per group, still the whole data volume passes through the disk).

Headless: no page, all update() calls involved are guarded/patched.
"""

import flet as ft


from ui.search_view import SearchView


def make_view(**kwargs):
    kwargs.setdefault("on_scan_start", lambda *a, **k: None)
    return SearchView(**kwargs)


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
