import atexit
import os
import sys
import sqlite3
import threading
from typing import Optional, Dict

from app_logging import get_logger

logger = get_logger(__name__)

def get_data_dir() -> str:
    """Writable directory for persistent app data.

    PyInstaller --onefile unpacks `__file__` into a temporary `_MEI*` folder that is
    wiped on exit, so the cache DB must live in the per-user profile when frozen.

    DUPLICATER_DATA_DIR overrides everything (checked first) — used by the test
    suite to keep runtime data isolated from the real user profile.
    """
    env_dir = os.environ.get("DUPLICATER_DATA_DIR")
    if env_dir:
        os.makedirs(env_dir, exist_ok=True)
        return env_dir

    if getattr(sys, "frozen", False):
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        data_dir = os.path.join(base, "Duplicater")
        os.makedirs(data_dir, exist_ok=True)
        return data_dir
    return os.path.dirname(os.path.abspath(__file__))

DB_PATH = os.path.join(get_data_dir(), "scan_cache.db")

class ScanCacheDB:
    """Thread-safe SQLite persistent hash cache for ultra-fast duplicate rescanning."""

    _instance = None
    _lock = threading.Lock()

    def __new__(cls, db_path: str = DB_PATH):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super(ScanCacheDB, cls).__new__(cls)
                cls._instance._init_db(db_path)
            return cls._instance

    def _init_db(self, db_path: str):
        self.db_path = db_path
        self._local = threading.local()
        with self._get_connection() as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS file_hashes (
                    path TEXT PRIMARY KEY,
                    size INTEGER NOT NULL,
                    mtime REAL NOT NULL,
                    full_hash TEXT,
                    turbo_hash TEXT
                );
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS image_phashes (
                    path TEXT PRIMARY KEY,
                    size INTEGER NOT NULL,
                    mtime REAL NOT NULL,
                    dhash TEXT NOT NULL,
                    ahash TEXT
                );
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_file_lookup ON file_hashes(size, mtime);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_img_lookup ON image_phashes(size, mtime);")

    def _get_connection(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            self._local.conn = sqlite3.connect(self.db_path, timeout=30.0, check_same_thread=False)
        return self._local.conn

    def close(self) -> None:
        """Close this thread's cached connection (2.2d).

        Connections are per-thread; a worker thread that finished its scan
        calls close() so the sqlite handle (and its WAL files) are released
        deterministically instead of waiting for the interpreter's GC. The
        next use on the same thread transparently opens a fresh connection.
        Idempotent — closing with no connection open on this thread is a
        no-op, and a failed close still drops the stale handle."""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass
            self._local.conn = None

    def get_file_hash(self, path: str, size: int, mtime: float, turbo: bool = False) -> Optional[str]:
        """Fetch cached hash if size and mtime match."""
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT size, mtime, full_hash, turbo_hash FROM file_hashes WHERE path = ?", (path,))
            row = cursor.fetchone()
            if row:
                cached_size, cached_mtime, full_h, turbo_h = row
                if cached_size == size and abs(cached_mtime - mtime) < 0.001:
                    # A turbo lookup must never fall back to the full hash:
                    # grouping keys would mix 16- and 64-char hashes and split
                    # identical files apart (round 3).
                    return turbo_h if turbo else full_h
        except Exception as ex:
            logger.debug("file hash cache lookup failed for %s: %s", path, ex)
        return None

    def save_file_hash(self, path: str, size: int, mtime: float, full_hash: Optional[str] = None, turbo_hash: Optional[str] = None):
        """Save or update file hash in cache.

        A cached hash is only valid for the (size, mtime) it was computed
        with. When the row is updated for a CHANGED file and the new write
        does not carry that hash type, the stale value is dropped — otherwise
        the full-SHA-256 verification phase would "confirm" an edited file as
        a duplicate of its own OLD content and preselect it for deletion
        (round 3: the single most dangerous data-loss path found)."""
        try:
            conn = self._get_connection()
            with conn:
                conn.execute("""
                    INSERT INTO file_hashes (path, size, mtime, full_hash, turbo_hash)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(path) DO UPDATE SET
                        size = excluded.size,
                        mtime = excluded.mtime,
                        full_hash = CASE
                            WHEN excluded.full_hash IS NOT NULL THEN excluded.full_hash
                            WHEN file_hashes.size != excluded.size
                                 OR file_hashes.mtime != excluded.mtime THEN NULL
                            ELSE file_hashes.full_hash
                        END,
                        turbo_hash = CASE
                            WHEN excluded.turbo_hash IS NOT NULL THEN excluded.turbo_hash
                            WHEN file_hashes.size != excluded.size
                                 OR file_hashes.mtime != excluded.mtime THEN NULL
                            ELSE file_hashes.turbo_hash
                        END;
                """, (path, size, mtime, full_hash, turbo_hash))
        except Exception as ex:
            logger.debug("file hash cache save failed for %s: %s", path, ex)

    def get_image_phash(self, path: str, size: int, mtime: float) -> Optional[str]:
        """Fetch cached perceptual dhash."""
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT size, mtime, dhash FROM image_phashes WHERE path = ?", (path,))
            row = cursor.fetchone()
            if row:
                cached_size, cached_mtime, dhash = row
                if cached_size == size and abs(cached_mtime - mtime) < 0.001:
                    return dhash
        except Exception as ex:
            logger.debug("image phash cache lookup failed for %s: %s", path, ex)
        return None

    def save_image_phash(self, path: str, size: int, mtime: float, dhash: str, ahash: Optional[str] = None):
        """Save or update perceptual hash."""
        try:
            conn = self._get_connection()
            with conn:
                conn.execute("""
                    INSERT INTO image_phashes (path, size, mtime, dhash, ahash)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(path) DO UPDATE SET
                        size = excluded.size,
                        mtime = excluded.mtime,
                        dhash = excluded.dhash,
                        ahash = excluded.ahash;
                """, (path, size, mtime, dhash, ahash))
        except Exception as ex:
            logger.debug("image phash cache save failed for %s: %s", path, ex)

    def get_cache_stats(self) -> Dict[str, int]:
        """Return total cached files count and cache file size on disk."""
        total_hashes = 0
        total_images = 0
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM file_hashes")
            total_hashes = cursor.fetchone()[0]
            cursor.execute("SELECT COUNT(*) FROM image_phashes")
            total_images = cursor.fetchone()[0]
        except Exception as ex:
            logger.debug("cache stats failed: %s", ex)

        db_size = os.path.getsize(self.db_path) if os.path.exists(self.db_path) else 0
        return {
            "total_files": total_hashes,
            "total_images": total_images,
            "size_bytes": db_size
        }

    def clear_cache(self) -> bool:
        """Wipe all cached hashes and vacuum database.

        Returns True only when the wipe actually succeeded — the caller
        (Settings) reports success/failure based on this (2.1b: a void
        return made the «cache cleared» dialog unconditional)."""
        try:
            conn = self._get_connection()
            with conn:
                conn.execute("DELETE FROM file_hashes;")
                conn.execute("DELETE FROM image_phashes;")
            conn.execute("VACUUM;")
            return True
        except Exception as ex:
            # User-initiated action: a silent failure here would leave the user
            # convinced the cache was wiped when it was not.
            logger.warning("cache clear failed: %s", ex)
            return False

cache_db = ScanCacheDB()

# Release the main thread's connection on a normal app exit: a clean sqlite
# close checkpoints and removes the WAL files (worker threads close theirs
# at scan end, see main.run_scan / main.run_sample_scan; 2.2d).
atexit.register(cache_db.close)
