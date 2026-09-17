"""Regression tests for the Этап-2 main.py fixes.

Covers the C2 TOCTOU re-verification helper and the H2 compare-results
fallback schema — the pure building blocks of the delete/hardlink/compare
handlers. Starting the real GUI is out of scope.

Data-safety: every file lives in the test's own tmp_path.
"""
import os
import threading
import time

import main
import scanner


class TestVerifyFileUnchanged:
    """C2: a file must be re-checked (size + mtime) before any destructive op —
    the tuple alone decides, never a fresh os.stat of the current state."""

    @staticmethod
    def _snapshot(path: str):
        stat = os.stat(path)
        return path, stat.st_size, stat.st_mtime

    def test_unchanged_file_passes(self, tmp_path):
        f = tmp_path / "same.bin"
        f.write_bytes(b"data" * 10)
        ok, reason = main.verify_file_unchanged(*self._snapshot(str(f)))
        assert ok is True
        assert reason == ""

    def test_size_change_fails(self, tmp_path):
        f = tmp_path / "grown.bin"
        f.write_bytes(b"a" * 10)
        path, size, mtime = self._snapshot(str(f))
        f.write_bytes(b"a" * 25)
        ok, reason = main.verify_file_unchanged(path, size, mtime)
        assert ok is False
        assert "size" in reason.lower()

    def test_mtime_change_fails(self, tmp_path):
        f = tmp_path / "touched.bin"
        f.write_bytes(b"a" * 10)
        path, size, mtime = self._snapshot(str(f))
        future = time.time() + 60
        os.utime(str(f), (future, future))
        ok, reason = main.verify_file_unchanged(path, size, mtime)
        assert ok is False
        assert "modified" in reason.lower()

    def test_missing_file_fails_with_reason(self, tmp_path):
        ok, reason = main.verify_file_unchanged(str(tmp_path / "gone.bin"), 10, 1234.5)
        assert ok is False
        assert reason  # non-empty reason ends up in the per-file error list

    def test_missing_file_does_not_raise(self, tmp_path):
        # A vanished file must be reported, never crash the worker.
        main.verify_file_unchanged(str(tmp_path / "vanished.bin"), 1, 0.0)


class TestCompareFallbackSchema:
    def test_fallback_has_same_keys_as_real_result(self, tmp_path):
        """H2: run_compare()'s error fallback must be renderable by
        compare_view — same keys as a real compare_folders() result."""
        a = tmp_path / "a"
        b = tmp_path / "b"
        a.mkdir()
        b.mkdir()
        (a / "f.txt").write_text("x")
        (b / "g.txt").write_text("y")

        real = scanner.compare_folders(str(a), str(b))
        assert set(main.EMPTY_COMPARE_RESULT.keys()) == set(real.keys())

    def test_fallback_values_are_empty(self):
        assert main.EMPTY_COMPARE_RESULT["unique_a"] == []
        assert main.EMPTY_COMPARE_RESULT["unique_b"] == []
        assert main.EMPTY_COMPARE_RESULT["common"] == []
        assert main.EMPTY_COMPARE_RESULT["total_files"] == 0


class TestBuildHardlinkLogPairs:
    """Round-2 regression: group entries are (path, size, mtime) tuples while
    succeeded paths are plain strings — the old direct comparison never
    matched, so hardlink operations were never journaled and undo was dead."""

    def test_only_actually_linked_pairs_are_journaled(self):
        groups_map = {
            "orig/a.jpg": [("dup1.jpg", 10, 1.0), ("dup2.jpg", 20, 2.0)],
            "orig/b.jpg": [("dup3.jpg", 30, 3.0)],
        }
        pairs = main.build_hardlink_log_pairs(groups_map, ["dup1.jpg", "dup3.jpg"])
        assert pairs == [("orig/a.jpg", "dup1.jpg"), ("orig/b.jpg", "dup3.jpg")]

    def test_tuple_entries_match_string_paths(self):
        groups_map = {"o.txt": [("d.txt", 1, 1.0)]}
        assert main.build_hardlink_log_pairs(groups_map, ["d.txt"]) == [("o.txt", "d.txt")]

    def test_nothing_succeeded_yields_no_pairs(self):
        groups_map = {"o.txt": [("d.txt", 1, 1.0)]}
        assert main.build_hardlink_log_pairs(groups_map, []) == []

    def test_dedupes_succeeded_paths(self):
        groups_map = {"o.txt": [("d.txt", 1, 1.0)]}
        assert main.build_hardlink_log_pairs(groups_map, ["d.txt", "d.txt"]) == [("o.txt", "d.txt")]


class TestHistoryConcurrency:
    """Round 2: scan-history writes raced with worker journaling — unlocked
    read-modify-save cycles dropped entries."""

    def test_concurrent_appends_keep_all_entries(self, monkeypatch, tmp_path):
        monkeypatch.setattr(main, "HISTORY_FILE", str(tmp_path / "history.json"))

        def worker(n):
            main.add_to_history([f"d{n}"], 1, 1)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        history = main.load_history()
        assert len(history) == 8
        assert {h["folders"][0] for h in history} == {f"d{n}" for n in range(8)}


class TestSettingsStore:
    """Round 2: language/theme/trash-default persist across launches (they
    used to reset to hardcoded values every start)."""

    def test_defaults_when_file_missing(self, monkeypatch, tmp_path):
        monkeypatch.setattr(main, "SETTINGS_FILE", str(tmp_path / "settings.json"))
        assert main.load_settings() == dict(main.DEFAULT_SETTINGS)

    def test_save_and_reload_roundtrip(self, monkeypatch, tmp_path):
        monkeypatch.setattr(main, "SETTINGS_FILE", str(tmp_path / "settings.json"))
        main.save_settings({"language": "en", "theme": "midnight_oled"})
        settings = main.load_settings()
        assert settings["language"] == "en"
        assert settings["theme"] == "midnight_oled"
        assert settings["trash_default"] is True  # untouched default survives

    def test_corrupt_file_falls_back_to_defaults(self, monkeypatch, tmp_path):
        p = tmp_path / "settings.json"
        p.write_text("{ this is not json", encoding="utf-8")
        monkeypatch.setattr(main, "SETTINGS_FILE", str(p))
        assert main.load_settings() == dict(main.DEFAULT_SETTINGS)

    def test_failed_atomic_save_keeps_previous_settings(self, monkeypatch, tmp_path):
        p = tmp_path / "settings.json"
        monkeypatch.setattr(main, "SETTINGS_FILE", str(p))
        main.save_settings({"language": "en"})

        def crash_replace(src, dst):
            raise OSError("simulated crash mid-save")

        monkeypatch.setattr("os.replace", crash_replace)
        main.save_settings({"language": "ru"})  # swallowed + logged
        assert main.load_settings()["language"] == "en"
