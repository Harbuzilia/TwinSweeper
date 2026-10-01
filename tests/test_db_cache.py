"""Tests for db_cache.py — SQLite hash cache, validation, thread safety."""
import os
import threading

import pytest

from db_cache import ScanCacheDB, get_data_dir, DB_PATH


class TestDataDirIsolation:
    def test_env_var_overrides_data_dir(self, isolated_data_dir):
        assert os.path.normcase(get_data_dir()) == os.path.normcase(isolated_data_dir)

    def test_db_path_is_inside_isolated_dir(self, isolated_data_dir):
        assert os.path.normcase(os.path.dirname(DB_PATH)) == os.path.normcase(isolated_data_dir)

    def test_real_project_dir_is_not_touched(self, isolated_data_dir):
        # The data dir must never resolve to the project root in tests.
        # Both sides normalized — the old un-normalized "tests\.." comparison
        # was string-unequal to ANY real path and proved nothing.
        project_root = os.path.normpath(
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
        )
        assert os.path.normcase(get_data_dir()) != os.path.normcase(project_root)
        # Positive control: it really is the throwaway dir.
        assert os.path.normcase(get_data_dir()) == os.path.normcase(isolated_data_dir)


class TestSingleton:
    def test_scan_cache_db_is_singleton(self):
        assert ScanCacheDB() is ScanCacheDB()


class TestFileHashCache:
    def test_save_and_get_full_hash_roundtrip(self, fresh_cache_db):
        fresh_cache_db.save_file_hash(r"C:\some\file.txt", 100, 12345.5, full_hash="abc123")
        assert fresh_cache_db.get_file_hash(r"C:\some\file.txt", 100, 12345.5) == "abc123"

    def test_save_and_get_turbo_hash_roundtrip(self, fresh_cache_db):
        fresh_cache_db.save_file_hash(r"C:\some\file.txt", 100, 12345.5, turbo_hash="turbo99")
        assert fresh_cache_db.get_file_hash(r"C:\some\file.txt", 100, 12345.5, turbo=True) == "turbo99"
        # Without turbo flag the full hash is requested — not stored yet.
        assert fresh_cache_db.get_file_hash(r"C:\some\file.txt", 100, 12345.5) is None

    def test_full_and_turbo_hash_coexist(self, fresh_cache_db):
        fresh_cache_db.save_file_hash(r"C:\f", 10, 1.0, full_hash="FULL")
        fresh_cache_db.save_file_hash(r"C:\f", 10, 1.0, turbo_hash="TURBO")
        assert fresh_cache_db.get_file_hash(r"C:\f", 10, 1.0) == "FULL"
        assert fresh_cache_db.get_file_hash(r"C:\f", 10, 1.0, turbo=True) == "TURBO"

    def test_unknown_path_returns_none(self, fresh_cache_db):
        assert fresh_cache_db.get_file_hash(r"C:\missing.txt", 1, 1.0) is None

    def test_size_mismatch_invalidates_cache(self, fresh_cache_db):
        fresh_cache_db.save_file_hash(r"C:\f", 100, 1.0, full_hash="H")
        assert fresh_cache_db.get_file_hash(r"C:\f", 200, 1.0) is None

    def test_mtime_mismatch_invalidates_cache(self, fresh_cache_db):
        fresh_cache_db.save_file_hash(r"C:\f", 100, 1.0, full_hash="H")
        assert fresh_cache_db.get_file_hash(r"C:\f", 100, 1.5) is None

    def test_update_overwrites_stale_entry(self, fresh_cache_db):
        fresh_cache_db.save_file_hash(r"C:\f", 100, 1.0, full_hash="OLD")
        fresh_cache_db.save_file_hash(r"C:\f", 300, 2.0, full_hash="NEW")
        assert fresh_cache_db.get_file_hash(r"C:\f", 300, 2.0) == "NEW"
        assert fresh_cache_db.get_file_hash(r"C:\f", 100, 1.0) is None

    def test_stale_full_hash_dropped_when_file_changed(self, fresh_cache_db):
        """Round 3 CRITICAL: saving a turbo hash for a CHANGED file (new
        size/mtime) must NOT keep the old full_hash alive — otherwise the
        SHA-256 verification phase 'confirms' an edited file as a duplicate
        of its own old content."""
        fresh_cache_db.save_file_hash(r"C:\f", 100, 1.0, full_hash="OLDFULL")
        # File changed; the scanner re-saves only the turbo hash.
        fresh_cache_db.save_file_hash(r"C:\f", 200, 2.0, turbo_hash="NEWTURBO")
        # Stale full hash must be gone under the new size/mtime...
        assert fresh_cache_db.get_file_hash(r"C:\f", 200, 2.0, turbo=False) is None
        # ...while the fresh turbo hash is present.
        assert fresh_cache_db.get_file_hash(r"C:\f", 200, 2.0, turbo=True) == "NEWTURBO"

    def test_stale_turbo_hash_dropped_when_file_changed(self, fresh_cache_db):
        fresh_cache_db.save_file_hash(r"C:\f", 100, 1.0, turbo_hash="OLDTURBO")
        fresh_cache_db.save_file_hash(r"C:\f", 200, 2.0, full_hash="NEWFULL")
        assert fresh_cache_db.get_file_hash(r"C:\f", 200, 2.0, turbo=True) is None
        assert fresh_cache_db.get_file_hash(r"C:\f", 200, 2.0, turbo=False) == "NEWFULL"

    def test_full_hash_survives_when_size_mtime_unchanged(self, fresh_cache_db):
        # Same size+mtime → the file is presumed unchanged → keep the hash.
        fresh_cache_db.save_file_hash(r"C:\f", 100, 1.0, full_hash="FULL")
        fresh_cache_db.save_file_hash(r"C:\f", 100, 1.0, turbo_hash="TURBO")
        assert fresh_cache_db.get_file_hash(r"C:\f", 100, 1.0, turbo=False) == "FULL"

    def test_turbo_lookup_never_returns_full_hash(self, fresh_cache_db):
        # Mixing a 64-char full hash into 16-char turbo grouping keys would
        # split identical files apart.
        fresh_cache_db.save_file_hash(r"C:\f", 100, 1.0, full_hash="FULL64CHARS")
        assert fresh_cache_db.get_file_hash(r"C:\f", 100, 1.0, turbo=True) is None


class TestImagePhashCache:
    def test_save_and_get_phash_roundtrip(self, fresh_cache_db):
        fresh_cache_db.save_image_phash(r"C:\img.jpg", 500, 9.0, "deadbeefdeadbeef")
        assert fresh_cache_db.get_image_phash(r"C:\img.jpg", 500, 9.0) == "deadbeefdeadbeef"

    def test_phash_invalidated_on_size_change(self, fresh_cache_db):
        fresh_cache_db.save_image_phash(r"C:\img.jpg", 500, 9.0, "deadbeefdeadbeef")
        assert fresh_cache_db.get_image_phash(r"C:\img.jpg", 501, 9.0) is None

    def test_phash_invalidated_on_mtime_change(self, fresh_cache_db):
        fresh_cache_db.save_image_phash(r"C:\img.jpg", 500, 9.0, "deadbeefdeadbeef")
        assert fresh_cache_db.get_image_phash(r"C:\img.jpg", 500, 10.0) is None


class TestStatsAndClear:
    def test_cache_stats_counts(self, fresh_cache_db):
        fresh_cache_db.save_file_hash(r"C:\a", 1, 1.0, full_hash="H1")
        fresh_cache_db.save_file_hash(r"C:\b", 2, 2.0, full_hash="H2")
        fresh_cache_db.save_image_phash(r"C:\c.jpg", 3, 3.0, "PH")
        stats = fresh_cache_db.get_cache_stats()
        assert stats["total_files"] == 2
        assert stats["total_images"] == 1
        assert stats["size_bytes"] > 0

    def test_clear_cache_wipes_everything(self, fresh_cache_db):
        fresh_cache_db.save_file_hash(r"C:\a", 1, 1.0, full_hash="H1")
        fresh_cache_db.save_image_phash(r"C:\c.jpg", 3, 3.0, "PH")
        fresh_cache_db.clear_cache()
        stats = fresh_cache_db.get_cache_stats()
        assert stats["total_files"] == 0
        assert stats["total_images"] == 0


class TestClearCacheResult:
    """2.1b: clear_cache must report whether the wipe actually happened —
    the Settings dialog used to say «Cache cleared» unconditionally, even
    when the operation failed."""

    def test_clear_cache_returns_true_on_success(self, fresh_cache_db):
        fresh_cache_db.save_file_hash(r"C:\a", 1, 1.0, full_hash="H1")
        assert fresh_cache_db.clear_cache() is True
        # And it really wiped:
        assert fresh_cache_db.get_file_hash(r"C:\a", 1, 1.0) is None

    def test_clear_cache_returns_false_on_failure(self, fresh_cache_db, monkeypatch):
        import sqlite3

        def broken_connection():
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(fresh_cache_db, "_get_connection", broken_connection)
        assert fresh_cache_db.clear_cache() is False


class TestCloseReleasesConnection:
    """2.2d: per-thread sqlite connections used to live until interpreter
    GC. close() releases this thread's handle deterministically; the next
    use transparently opens a fresh, working connection."""

    def test_close_releases_and_reuse_creates_new_working_connection(self, fresh_cache_db):
        import sqlite3

        fresh_cache_db.save_file_hash(r"C:\f", 10, 1.0, full_hash="H")
        conn = fresh_cache_db._local.conn
        assert conn is not None

        fresh_cache_db.close()

        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")
        assert fresh_cache_db._local.conn is None

        # Reuse after close: a fresh connection is opened and works.
        assert fresh_cache_db.get_file_hash(r"C:\f", 10, 1.0) == "H"
        assert fresh_cache_db._local.conn is not None
        assert fresh_cache_db._local.conn is not conn

    def test_close_without_connection_is_noop(self, fresh_cache_db):
        fresh_cache_db.close()  # this thread never opened one
        assert fresh_cache_db.get_file_hash(r"C:\missing", 1, 1.0) is None


class TestThreadSafety:
    def test_concurrent_writes_all_persisted(self, fresh_cache_db):
        n_threads, n_writes = 8, 25

        def writer(tid):
            for i in range(n_writes):
                fresh_cache_db.save_file_hash(
                    rf"C:\t{tid}\f{i}.txt", i, float(i), full_hash=f"h{tid}_{i}"
                )

        threads = [threading.Thread(target=writer, args=(t,)) for t in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        for tid in range(n_threads):
            for i in range(n_writes):
                assert fresh_cache_db.get_file_hash(
                    rf"C:\t{tid}\f{i}.txt", i, float(i)
                ) == f"h{tid}_{i}"
