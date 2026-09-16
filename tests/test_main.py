"""Regression tests for the Этап-2 main.py fixes.

Covers the C2 TOCTOU re-verification helper and the H2 compare-results
fallback schema — the pure building blocks of the delete/hardlink/compare
handlers. Starting the real GUI is out of scope.

Data-safety: every file lives in the test's own tmp_path.
"""
import os
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
