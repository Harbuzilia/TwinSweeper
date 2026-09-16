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
        assert os.path.normcase(get_data_dir()) != os.path.normcase(
            os.path.dirname(os.path.abspath(__file__)) + os.sep + ".."
        )


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
