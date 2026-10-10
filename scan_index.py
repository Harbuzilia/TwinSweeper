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
* both stream whole size-groups lazily (``iter_candidate_groups``) in
  discovery order (``ORDER BY size, rowid`` == the old ``defaultdict``
  insertion order), so ``FileInfo`` objects are constructed only for
  candidate files, group by group.

Rows are plain tuples ``(path, name, size, created, modified)`` — the exact
constructor fields of ``scanner.FileInfo``. ``FileInfo`` is deliberately NOT
imported here: the caller builds the objects, keeping this module free of a
circular import and of any per-row object overhead.

The spill DB is transient scan state (a crash simply means "rescan"), hence
``journal_mode=MEMORY``/``synchronous=OFF`` for insert speed — unlike the
persistent hash cache in ``db_cache`` which uses WAL.
"""
import os
import sqlite3
import tempfile
from typing import Dict, Iterator, List, Optional, Tuple

from app_logging import get_logger

logger = get_logger(__name__)

# Row = (path, name, size, created, modified)
Row = Tuple[str, str, int, float, float]

# Rows buffered in RAM before one executemany flush into the spill DB.
INSERT_BATCH_ROWS = 5000


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
    """Tuple-row in-RAM index — fallback when the SQLite spill is unavailable."""

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
        dup = [rows for rows in self._by_size.values() if len(rows) > 1]
        return len(dup), sum(len(rows) for rows in dup)

    def iter_candidate_groups(self, by_size: bool = True) -> Iterator[List[Row]]:
        if not by_size:
            # Insertion order — identical to SqliteScanIndex's ORDER BY rowid,
            # so the fallback index is interchangeable with the spill.
            if len(self._all) > 1:
                yield list(self._all)
            return
        for rows in self._by_size.values():
            if len(rows) > 1:
                yield rows

    def close(self) -> None:
        self._by_size.clear()
        self._all.clear()
        self.closed = True


class SqliteScanIndex(ScanIndex):
    """Spill-to-disk discovery index: rows live in a throwaway SQLite DB, and
    candidate groups are streamed back through a lazy cursor."""

    def __init__(self, db_path: Optional[str] = None) -> None:
        self.closed = False
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
        self._conn.executemany("INSERT INTO idx (path, name, size, created, modified) VALUES (?, ?, ?, ?, ?)", self._buffer)
        self._conn.commit()
        self._buffer.clear()

    def finish(self) -> None:
        self._flush()

    def candidate_stats(self, by_size: bool = True) -> Tuple[int, int]:
        if not by_size:
            total = self._conn.execute("SELECT COUNT(*) FROM idx").fetchone()[0]
            return (1, total) if total > 1 else (0, 0)
        row = self._conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(cnt), 0) FROM "
            "(SELECT COUNT(*) AS cnt FROM idx GROUP BY size HAVING COUNT(*) > 1)"
        ).fetchone()
        return int(row[0]), int(row[1])

    def iter_candidate_groups(self, by_size: bool = True) -> Iterator[List[Row]]:
        if not by_size:
            total = self._conn.execute("SELECT COUNT(*) FROM idx").fetchone()[0]
            if total <= 1:
                return
            cur = self._conn.execute("SELECT path, name, size, created, modified FROM idx ORDER BY rowid")
            yield [tuple(r) for r in cur]
            return
        # rowid == insert order, so rows within a size group keep discovery
        # order exactly like the old defaultdict(list) index did.
        cur = self._conn.execute(
            "SELECT size, path, name, created, modified FROM idx "
            "WHERE size IN (SELECT size FROM idx GROUP BY size HAVING COUNT(*) > 1) "
            "ORDER BY size, rowid"
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
    try:
        return SqliteScanIndex(db_path)
    except (sqlite3.Error, OSError) as ex:
        logger.warning("scan index spill unavailable (%s); falling back to in-memory index", ex)
        return MemoryScanIndex()
