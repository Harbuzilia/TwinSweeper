"""Tests for scan_index.py — the 2.2f spill-to-disk discovery index.

Contract: both implementations (in-memory rows and SQLite spill) must agree
exactly on candidate stats and on the groups they stream, so scan_directory
can fall back between them without any behaviour change.
"""
import sqlite3

import pytest

import scan_index
from scan_index import MemoryScanIndex, SqliteScanIndex, open_scan_index

# (path, name, size, created, modified) — the row shape FileInfo is built from.
ROWS = [
    (r"C:\d\a.bin", "a.bin", 100, 1.0, 10.5),
    (r"C:\d\b.bin", "b.bin", 100, 2.0, 20.25),   # dup size with a
    (r"C:\d\u.bin", "u.bin", 101, 3.0, 30.0),    # unique size
    (r"C:\d\c.bin", "c.bin", 100, 4.0, 40.125),  # dup size
    (r"C:\d\v.bin", "v.bin", 202, 5.0, 50.0),    # unique size
]
DUP_GROUP = [ROWS[0], ROWS[1], ROWS[3]]


@pytest.fixture(params=["memory", "sqlite"])
def index(request, tmp_path):
    if request.param == "memory":
        idx = MemoryScanIndex()
    else:
        idx = SqliteScanIndex(str(tmp_path / "idx.db"))
    try:
        yield idx
    finally:
        idx.close()


def _fill(idx, rows=ROWS):
    for path, name, size, created, modified in rows:
        idx.add(path, name, size, created, modified)
    idx.finish()


class TestIndexContract:
    def test_candidate_stats_by_size(self, index):
        _fill(index)
        assert index.candidate_stats(by_size=True) == (1, 3)

    def test_candidate_stats_combined(self, index):
        _fill(index)
        # by_size=False treats every row as one candidate pool.
        assert index.candidate_stats(by_size=False) == (1, 5)

    def test_groups_contain_only_duplicate_sizes_in_insert_order(self, index):
        _fill(index)
        groups = list(index.iter_candidate_groups(by_size=True))
        assert groups == [DUP_GROUP]

    def test_interleaved_sizes_keep_insert_order_within_group(self, index):
        rows = [ROWS[0], ROWS[2], ROWS[1], ROWS[4], ROWS[3]]
        _fill(index, rows)
        groups = list(index.iter_candidate_groups(by_size=True))
        assert groups == [DUP_GROUP]

    def test_combined_mode_yields_single_group_of_all_rows(self, index):
        _fill(index)
        groups = list(index.iter_candidate_groups(by_size=False))
        assert groups == [ROWS]

    def test_field_roundtrip_is_exact(self, index):
        _fill(index)
        (group,) = list(index.iter_candidate_groups(by_size=True))
        # REAL is an IEEE-754 double: created/modified must survive bit-exact,
        # the mtime equality drives the hash-cache lookups later.
        assert group == DUP_GROUP

    def test_empty_index(self, index):
        index.finish()
        assert index.candidate_stats(by_size=True) == (0, 0)
        assert index.candidate_stats(by_size=False) == (0, 0)
        assert list(index.iter_candidate_groups(by_size=True)) == []
        assert list(index.iter_candidate_groups(by_size=False)) == []

    def test_single_row_combined_has_no_candidates(self, index):
        _fill(index, [ROWS[2]])
        assert index.candidate_stats(by_size=False) == (0, 0)
        assert list(index.iter_candidate_groups(by_size=False)) == []

    def test_all_unique_sizes_no_candidates(self, index):
        _fill(index, [ROWS[2], ROWS[4]])
        assert index.candidate_stats(by_size=True) == (0, 0)
        assert list(index.iter_candidate_groups(by_size=True)) == []

    def test_iteration_is_repeatable(self, index):
        _fill(index)
        first = list(index.iter_candidate_groups(by_size=True))
        second = list(index.iter_candidate_groups(by_size=True))
        assert first == second


class TestSqliteSpill:
    def test_batch_flush_boundary(self, monkeypatch, tmp_path):
        """Rows must survive the internal batch-flush boundary exactly."""
        monkeypatch.setattr(scan_index, "INSERT_BATCH_ROWS", 7)
        idx = SqliteScanIndex(str(tmp_path / "idx.db"))
        try:
            rows = [(str(tmp_path / f"f{i}.bin"), f"f{i}.bin", 500, float(i), float(i)) for i in range(20)]
            _fill(idx, rows)
            assert idx.candidate_stats(by_size=True) == (1, 20)
            (group,) = list(idx.iter_candidate_groups(by_size=True))
            assert group == rows
        finally:
            idx.close()

    def test_close_removes_own_temp_file(self):
        idx = SqliteScanIndex()
        path = idx.db_path
        idx.add("p", "p", 1, 0.0, 0.0)
        idx.finish()
        assert path is not None
        idx.close()
        import os
        assert not os.path.exists(path)

    def test_close_is_idempotent(self):
        idx = SqliteScanIndex()
        idx.close()
        idx.close()  # must not raise

    def test_explicit_path_survives_close(self, tmp_path):
        import os
        p = tmp_path / "idx.db"
        idx = SqliteScanIndex(str(p))
        idx.close()
        assert os.path.exists(str(p))

    def test_failed_connect_cleans_own_temp_file(self, monkeypatch):
        import os
        captured = {}
        real_connect = sqlite3.connect

        def failing_connect(path, *args, **kwargs):
            captured["path"] = path
            raise sqlite3.OperationalError("no disk")

        monkeypatch.setattr(scan_index.sqlite3, "connect", failing_connect)
        with pytest.raises(sqlite3.OperationalError):
            SqliteScanIndex()
        assert "path" in captured
        assert not os.path.exists(captured["path"])
        monkeypatch.setattr(scan_index.sqlite3, "connect", real_connect)


class TestOpenScanIndexFallback:
    def test_falls_back_to_memory_when_sqlite_fails(self, monkeypatch):
        def boom(*args, **kwargs):
            raise sqlite3.OperationalError("unavailable")

        monkeypatch.setattr(scan_index.sqlite3, "connect", boom)
        idx = open_scan_index()
        assert isinstance(idx, MemoryScanIndex)
        idx.close()

    def test_returns_sqlite_index_normally(self):
        idx = open_scan_index()
        try:
            assert isinstance(idx, SqliteScanIndex)
        finally:
            path = getattr(idx, "db_path", None)
            idx.close()
        if path:
            import os
            assert not os.path.exists(path)
