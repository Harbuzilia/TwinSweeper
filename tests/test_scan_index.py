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

    def test_groups_follow_size_first_appearance_order(self, index):
        """L1/L2: both backends agree — groups stream in the order their size
        first appeared in the walk, NOT in ascending size order. Rows inside a
        group stay in discovery order."""
        b1 = (r"C:\d\b1.bin", "b1.bin", 500, 1.0, 1.0)
        a1 = (r"C:\d\a1.bin", "a1.bin", 100, 2.0, 2.0)
        b2 = (r"C:\d\b2.bin", "b2.bin", 500, 3.0, 3.0)
        a2 = (r"C:\d\a2.bin", "a2.bin", 100, 4.0, 4.0)
        _fill(index, [b1, a1, b2, a2])
        groups = list(index.iter_candidate_groups(by_size=True))
        # size 500 was seen first (B, A, B, A) — it must come out first even
        # though 100 < 500.
        assert groups == [[b1, b2], [a1, a2]]

    def test_iteration_is_repeatable(self, index):
        _fill(index)
        first = list(index.iter_candidate_groups(by_size=True))
        second = list(index.iter_candidate_groups(by_size=True))
        assert first == second


class TestSqliteSpill:
    def test_spill_needs_no_sqlite_temp_files(self, tmp_path):
        """ORDER BY / CREATE INDEX must not spill to SQLite's own temp dir:
        a locked-down or full %TEMP% used to abort mid-iteration with
        'unable to open database file' (caught by the 50k benchmark).
        temp_store=MEMORY + the idx_first index (finish()) keep the streaming
        query on the index order — no sorter, no temp files. A sorter would
        also mean RAM proportional to the candidates, which is what the 2.2f
        memory ceiling exists to avoid."""
        idx = SqliteScanIndex(str(tmp_path / "idx.db"))
        try:
            mode = idx._conn.execute("PRAGMA temp_store").fetchone()[0]
            assert mode == 2, "temp_store must be MEMORY"
            for i in range(10):
                idx.add(f"p{i}", f"p{i}", 100, float(i), float(i))
            idx.finish()
            plan = [
                r[3] for r in idx._conn.execute(
                    "EXPLAIN QUERY PLAN "
                    "SELECT size, path, name, created, modified FROM idx "
                    "WHERE first_rowid IS NOT NULL ORDER BY first_rowid, rowid"
                )
            ]
            assert not any("TEMP B-TREE" in step for step in plan), plan
            names = [
                r[0] for r in idx._conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='idx'"
                )
            ]
            assert names, "finish() must create the indexes iteration reads (idx_size, idx_first)"
            (group,) = list(idx.iter_candidate_groups(by_size=True))
            assert len(group) == 10
        finally:
            idx.close()

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

    def test_flush_error_is_reraised_with_actionable_message(self, monkeypatch, tmp_path):
        """M1: a spill write failure mid-walk must not reach the UI as a bare
        'database is locked' — the scan aborts with a message that names the
        temp location, and the original sqlite error stays chained."""
        monkeypatch.setattr(scan_index, "INSERT_BATCH_ROWS", 1)
        idx = SqliteScanIndex(str(tmp_path / "idx.db"))

        class FailingConn:
            def executemany(self, *args, **kwargs):
                raise sqlite3.OperationalError("database is locked")

            def commit(self):
                pass

            def close(self):
                pass

        real_conn = idx._conn
        idx._conn = FailingConn()
        try:
            with pytest.raises(RuntimeError) as excinfo:
                idx.add("p", "p", 1, 0.0, 0.0)
        finally:
            idx._conn = real_conn
            idx.close()
        message = str(excinfo.value)
        assert "%TEMP%" in message
        assert "database is locked" in message
        assert isinstance(excinfo.value.__cause__, sqlite3.OperationalError)

    def test_finish_flush_error_is_reraised_with_actionable_message(self, monkeypatch, tmp_path):
        """The batch still buffered when the walk ends hits the same guard
        through finish(), not only through a mid-walk add()."""
        idx = SqliteScanIndex(str(tmp_path / "idx.db"))

        class FailingConn:
            def executemany(self, *args, **kwargs):
                raise sqlite3.OperationalError("disk I/O error")

            def commit(self):
                pass

            def close(self):
                pass

        real_conn = idx._conn
        idx._conn = FailingConn()
        idx.add("p", "p", 1, 0.0, 0.0)  # stays buffered (batch = 5000 rows)
        try:
            with pytest.raises(RuntimeError, match="%TEMP%"):
                idx.finish()
        finally:
            idx._conn = real_conn
            idx.close()

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


class TestStaleTempCleanup:
    def test_stale_spill_files_are_swept_only(self, tmp_path, monkeypatch, caplog):
        """L3: a killed process never reaches close(), so its spill DB stays
        in %TEMP%. The next open_scan_index() sweeps files older than the
        documented age, leaves fresh ones (a live scan) and anything that is
        not ours alone."""
        import logging
        import os
        import time

        old = time.time() - (scan_index.STALE_SCAN_INDEX_AGE_SECONDS + 60)
        stale = tmp_path / "twinsweeper_scanidx_stale.db"
        stale.write_bytes(b"x")
        os.utime(str(stale), (old, old))
        fresh = tmp_path / "twinsweeper_scanidx_fresh.db"
        fresh.write_bytes(b"x")
        foreign = tmp_path / "someone_elses.db"
        foreign.write_bytes(b"x")
        os.utime(str(foreign), (old, old))

        monkeypatch.setattr(scan_index.tempfile, "gettempdir", lambda: str(tmp_path))
        with caplog.at_level(logging.WARNING):
            idx = open_scan_index(str(tmp_path / "own.db"))
        idx.close()

        assert not stale.exists(), "an abandoned spill file must be removed"
        assert fresh.exists(), "another live scan's index must survive"
        assert foreign.exists(), "only twinsweeper_scanidx_*.db files are ours"
        assert any("stale scan index" in r.message for r in caplog.records)

    def test_cleanup_survives_a_broken_temp_dir(self, monkeypatch):
        """Cleanup is best effort: an unreadable %TEMP% must not break a scan
        that otherwise works (the spill itself reports its own failure)."""
        def boom(*args, **kwargs):
            raise OSError("no temp dir")

        monkeypatch.setattr(scan_index.tempfile, "gettempdir", boom)
        assert scan_index._cleanup_stale_temp_files() == 0


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
