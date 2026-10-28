"""Temporary discovery index for the duplicate scan (2.2f memory ceiling).

Phase 1 of ``scanner.scan_directory`` used to materialise every walked file
as a ``FileInfo`` in a ``defaultdict`` — hundreds of thousands of objects
(hundreds of MB) alive for the WHOLE scan, even though only files sharing a
size are ever needed again. This module keeps the discovery rows
``(path, name, size, created, modified)`` instead:

* ``SqliteScanIndex`` spills the rows to a throwaway on-disk SQLite DB in
  batches (the project already relies on SQLite in ``db_cache``), so the
  walk's RAM stays O(batch) regardless of tree size;
* ``MemoryScanIndex`` is the tuple-row fallback when SQLite is unavailable —
  still ~4x lighter per file than a full ``FileInfo``;
* both stream whole size-groups lazily (``iter_candidate_groups``) in the
  old ``defaultdict`` order: groups by the first appearance of their size in
  the walk, rows inside a group in discovery order. ``FileInfo`` objects are
  built only for candidate files, group by group, and the backend in use
  never changes the order of the scan's results.

Rows are plain tuples ``(path, name, size, created, modified)`` — the exact
constructor fields of ``scanner.FileInfo``. ``FileInfo`` is deliberately NOT
imported here: the caller builds the objects, keeping this module free of a
circular import and of any per-row object overhead.

The spill DB is transient scan state (a crash simply means "rescan"), hence
``journal_mode=MEMORY``/``synchronous=OFF`` for insert speed — unlike the
persistent hash cache in ``db_cache`` which uses WAL. A process killed before
``close()`` leaves its file in %TEMP%; ``open_scan_index`` sweeps spill files
older than ``STALE_SCAN_INDEX_AGE_SECONDS`` so they never pile up.
"""
import functools
import glob
import inspect
import os
import sqlite3
import tempfile
import time
from typing import Dict, Iterator, List, Optional, Tuple

from app_logging import get_logger
from locales import get_text

logger = get_logger(__name__)

# Row = (path, name, size, created, modified)
Row = Tuple[str, str, int, float, float]

# Rows buffered in RAM before one executemany flush into the spill DB.
INSERT_BATCH_ROWS = 5000

# Name of the throwaway spill DBs in %TEMP% (prefix of the mkstemp name).
_TEMP_DB_GLOB = "twinsweeper_scanidx_*.db"

# A spill DB is removed by close(); a process killed without unwinding leaves
# it behind. Only files older than this are swept: a live scan keeps rewriting
# its index, so its mtime stays fresh, and even a multi-hour scan on a slow
# disk never looks abandoned. 24h keeps a parallel run of the app (or a pause
# in a debugger overnight) safe.
STALE_SCAN_INDEX_AGE_SECONDS = 24 * 60 * 60


def _cleanup_stale_temp_files(max_age_seconds: int = STALE_SCAN_INDEX_AGE_SECONDS) -> int:
    """Delete spill DBs left in %TEMP% by a process that never reached close().

    Best effort: the files are transient scan artifacts, never user data, so
    any filesystem error simply leaves one alone. Returns the number removed.
    """
    removed = 0
    now = time.time()
    try:
        candidates = glob.glob(os.path.join(tempfile.gettempdir(), _TEMP_DB_GLOB))
    except (OSError, ValueError):
        return 0
    for path in candidates:
        try:
            age_seconds = now - os.path.getmtime(path)
            if age_seconds <= max_age_seconds:
                continue
            os.remove(path)
        except OSError:
            continue
        removed += 1
        logger.warning(
            "removed stale scan index %s (%.0f h old, left by an interrupted scan)",
            path,
            age_seconds / 3600,
        )
    return removed


def _m1_guard(fn):
    """M1: surface a failing spill QUERY as the actionable %TEMP% message.

    ``_flush`` and ``finish`` guard the writes of the spill; the read side
    (candidate stats and candidate iteration, both called by ``scan_directory``
    mid-scan) needs the same guard, or a dead/sealed connection reaches the UI
    as a bare 'disk I/O error' / 'Cannot operate on a closed database'.
    The wrapper only translates the error: the message is the one the user can
    act on, and the sqlite error stays chained (``__cause__``) for the log.

    A generator function raises while it is being iterated, i.e. after the
    wrapper already returned, so its generator is drained with ``yield from``
    inside the guard.
    """

    def _fail(ex: sqlite3.Error) -> None:
        logger.error("scan index query failed: %s", ex)
        raise RuntimeError(get_text("scan_index_spill_error").format(ex)) from ex

    if inspect.isgeneratorfunction(fn):
        @functools.wraps(fn)
        def gen_wrapper(*args, **kwargs):
            try:
                yield from fn(*args, **kwargs)
            except sqlite3.Error as ex:
                _fail(ex)
        return gen_wrapper

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except sqlite3.Error as ex:
            _fail(ex)

    return wrapper


class ScanIndex:
    """Common contract of the discovery indexes (see module docstring)."""

    def add(self, path: str, name: str, size: int, created: float, modified: float) -> None:
        raise NotImplementedError

    def finish(self) -> None:
        """Seal the index after the walk; required before stats/iteration."""
        raise NotImplementedError

    def candidate_stats(self, by_size: bool = True) -> Tuple[int, int]:
        """(number of candidate groups, number of candidate files)."""
        raise NotImplementedError

    def iter_candidate_groups(self, by_size: bool = True) -> Iterator[List[Row]]:
        """Stream candidate groups lazily, in size-discovery order; rows in
        insert order. ``by_size=False`` yields ONE group with every row
        (the old all-candidates pool for name/hash-only scans)."""
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError


class MemoryScanIndex(ScanIndex):
    """Tuple-row in-RAM index — fallback when the SQLite spill is unavailable.

    Both buckets hold **lists**, not sets, and their order is load-bearing:

    * ``_by_size[size]`` keeps a size group's rows in discovery (insert) order.
      That order is part of the cross-backend contract:
      ``SqliteScanIndex`` streams ``ORDER BY first_rowid, rowid`` — groups by
      the first appearance of their size, rows in discovery order — so a scan
      yields identical groups on either backend.
    * ``_all`` holds every row in the same discovery order and backs the
      ``by_size=False`` pool (one group of all rows, matching the spill's
      ``ORDER BY rowid``).
    * Set semantics would be wrong twice over: a set loses insertion order
      (group order is visible in the results), and it silently collapses equal
      rows, which would move ``candidate_stats`` counts and the progress
      display built on them. The index does not need that safety net — rows
      are unique per path because the walk de-dups inodes before ``add()``.
    * The lists are owned by the index: ``iter_candidate_groups`` hands out the
      live list of a size group (the ``by_size=False`` pool is copied), so a
      caller must treat the yielded rows as read-only.
    """

    def __init__(self) -> None:
        self._by_size: Dict[int, List[Row]] = {}
        self._all: List[Row] = []
        self.closed = False

    def add(self, path: str, name: str, size: int, created: float, modified: float) -> None:
        row = (path, name, size, created, modified)
        self._by_size.setdefault(size, []).append(row)
        self._all.append(row)

    def finish(self) -> None:
        pass

    def candidate_stats(self, by_size: bool = True) -> Tuple[int, int]:
        if not by_size:
            total = len(self._all)
            return (1, total) if total > 1 else (0, 0)
        # L5: one pass over the size buckets into two counters — the old list
        # comprehension of duplicate groups cost O(#groups) pointers on every
        # call, the one allocation this index cannot afford to make worse than
        # the SQLite spill it stands in for.
        groups = 0
        files = 0
        for rows in self._by_size.values():
            if len(rows) > 1:
                groups += 1
                files += len(rows)
        return groups, files

    def iter_candidate_groups(self, by_size: bool = True) -> Iterator[List[Row]]:
        if not by_size:
            # Insertion order — identical to SqliteScanIndex's ORDER BY rowid,
            # so the fallback index is interchangeable with the spill.
            if len(self._all) > 1:
                yield list(self._all)
            return
        for rows in self._by_size.values():
            # dict insertion order == the size's first appearance in the walk,
            # which is exactly what SqliteScanIndex streams back too.
            if len(rows) > 1:
                yield rows

    def close(self) -> None:
        self._by_size.clear()
        self._all.clear()
        self.closed = True


class SqliteScanIndex(ScanIndex):
    """Spill-to-disk discovery index: rows live in a throwaway SQLite DB, and
    candidate groups are streamed back lazily, one ordered index scan."""

    def __init__(self, db_path: Optional[str] = None) -> None:
        self.closed = False
        self.sealed = False
        self._owns_file = db_path is None
        self._buffer: List[Row] = []
        if db_path is None:
            fd, db_path = tempfile.mkstemp(prefix="twinsweeper_scanidx_", suffix=".db")
            os.close(fd)
        self.db_path = db_path
        try:
            self._conn = sqlite3.connect(db_path)
            self._conn.execute("PRAGMA journal_mode=MEMORY;")
            self._conn.execute("PRAGMA synchronous=OFF;")
            # Sorter/index-build must never touch SQLite's own temp dir: a
            # locked-down or full %TEMP% aborts the streaming query with
            # 'unable to open database file' (caught by the 50k benchmark).
            self._conn.execute("PRAGMA temp_store=MEMORY;")
            self._conn.execute(
                "CREATE TABLE idx ("
                " size INTEGER NOT NULL,"
                " path TEXT NOT NULL,"
                " name TEXT NOT NULL,"
                " created REAL NOT NULL,"
                " modified REAL NOT NULL)"
            )
        except (sqlite3.Error, OSError):
            conn = getattr(self, "_conn", None)
            if conn is not None:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
            self._cleanup_file()
            raise

    def _cleanup_file(self) -> None:
        if self._owns_file:
            try:
                os.remove(self.db_path)
            except OSError:
                pass

    def add(self, path: str, name: str, size: int, created: float, modified: float) -> None:
        self._buffer.append((path, name, size, created, modified))
        if len(self._buffer) >= INSERT_BATCH_ROWS:
            self._flush()

    def _flush(self) -> None:
        if not self._buffer:
            return
        # Parameterised executemany — never string-built SQL.
        try:
            self._conn.executemany("INSERT INTO idx (path, name, size, created, modified) VALUES (?, ?, ?, ?, ?)", self._buffer)
            self._conn.commit()
        except sqlite3.Error as ex:
            # A dead spill write (disk full, permissions, locked file) must not
            # abort the scan with a bare 'database is locked' (M1): name the
            # temp location so the user can act on it. main.py renders the
            # exception message verbatim; the sqlite error stays chained for
            # logging/debugging.
            logger.error("scan index spill write failed: %s", ex)
            raise RuntimeError(get_text("scan_index_spill_error").format(ex)) from ex
        self._buffer.clear()

    def finish(self) -> None:
        self._flush()
        if self.sealed:
            return
        # The seal is the heaviest write of the spill (two index builds, a
        # full-table UPDATE, a table rewrite), so it gets the same M1 guard as
        # _flush(): a dead write must reach the user as the actionable message
        # naming %TEMP%, not as a bare 'database or disk is full'. `sealed` is
        # set only after the seal commits: on failure the partial transaction
        # is rolled back and the index stays unsealed, so a repeated finish()
        # retries the seal (each step is idempotent) instead of leaving a
        # half-sealed table that iteration cannot read ('no such column:
        # first_rowid').
        # Sealing turns discovery order into an index order, once, after the
        # bulk load (inserting into an unindexed table first is several times
        # faster):
        #  * idx_size  — GROUP BY size / MIN(rowid) below (and candidate_stats);
        #  * dup       — one row per duplicate size: the rowid of its first
        #                occurrence, i.e. the group's place in the walk;
        #  * first_rowid — that place copied onto every candidate row, NULL for
        #                unique-size rows (never streamed);
        #  * idx_first — the streaming scan's order: `first_rowid, rowid` is
        #                exactly "groups by first appearance of their size, rows
        #                in discovery order", served by the index with no sorter
        #                and no temp files (2.2f memory ceiling).
        try:
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_size ON idx(size)")
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS dup (size INTEGER PRIMARY KEY, first_rowid INTEGER NOT NULL)"
            )
            self._conn.execute(
                "INSERT OR REPLACE INTO dup (size, first_rowid) "
                "SELECT size, MIN(rowid) FROM idx GROUP BY size HAVING COUNT(*) > 1"
            )
            # ADD COLUMN is the one non-repeatable step: a retry after a partial
            # seal (DDL autocommitted before the failure) must not die on
            # 'duplicate column name'.
            columns = {row[1] for row in self._conn.execute("PRAGMA table_info(idx)")}
            if "first_rowid" not in columns:
                self._conn.execute("ALTER TABLE idx ADD COLUMN first_rowid INTEGER")
            self._conn.execute(
                "UPDATE idx SET first_rowid = (SELECT first_rowid FROM dup WHERE dup.size = idx.size)"
            )
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_first ON idx(first_rowid)")
            self._conn.execute("DROP TABLE IF EXISTS dup")
            self._conn.commit()
        except sqlite3.Error as ex:
            try:
                self._conn.rollback()
            except sqlite3.Error:
                pass
            logger.error("scan index seal failed: %s", ex)
            raise RuntimeError(get_text("scan_index_spill_error").format(ex)) from ex
        self.sealed = True

    @_m1_guard
    def candidate_stats(self, by_size: bool = True) -> Tuple[int, int]:
        if not by_size:
            total = self._conn.execute("SELECT COUNT(*) FROM idx").fetchone()[0]
            return (1, total) if total > 1 else (0, 0)
        row = self._conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(cnt), 0) FROM "
            "(SELECT COUNT(*) AS cnt FROM idx GROUP BY size HAVING COUNT(*) > 1)"
        ).fetchone()
        return int(row[0]), int(row[1])

    @_m1_guard
    def iter_candidate_groups(self, by_size: bool = True) -> Iterator[List[Row]]:
        if not by_size:
            total = self._conn.execute("SELECT COUNT(*) FROM idx").fetchone()[0]
            if total <= 1:
                return
            cur = self._conn.execute("SELECT path, name, size, created, modified FROM idx ORDER BY rowid")
            yield [tuple(r) for r in cur]
            return
        # L1/L2: `first_rowid, rowid` is the size-discovery order of the old
        # defaultdict(list) index — groups by the first appearance of their
        # size, rows in discovery order — and it is an index scan, not a
        # sorter, so the ordering costs no RAM and no temp files. Consecutive
        # rows always share a size, so a size change marks a group boundary.
        cur = self._conn.execute(
            "SELECT size, path, name, created, modified FROM idx "
            "WHERE first_rowid IS NOT NULL ORDER BY first_rowid, rowid"
        )
        group: List[Row] = []
        current_size = None
        for size, path, name, created, modified in cur:
            if size != current_size:
                if group:
                    yield group
                group = []
                current_size = size
            group.append((path, name, size, created, modified))
        if group:
            yield group

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self._buffer.clear()
        try:
            self._conn.close()
        except sqlite3.Error:
            pass
        self._cleanup_file()


def open_scan_index(db_path: Optional[str] = None) -> ScanIndex:
    """SQLite spill when possible, in-RAM tuple rows when not: an unwritable
    temp location must never break a scan that worked before 2.2f."""
    _cleanup_stale_temp_files()
    try:
        return SqliteScanIndex(db_path)
    except (sqlite3.Error, OSError) as ex:
        logger.warning("scan index spill unavailable (%s); falling back to in-memory index", ex)
        return MemoryScanIndex()
