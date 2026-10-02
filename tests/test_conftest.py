"""Tests for tests/conftest.py — isolation guarantees of shared fixtures.

RELEASE_PLAN 3.1: the ``fresh_cache_db`` fixture must substitute not only the
``ScanCacheDB._instance`` class singleton but also the module-level ``cache_db``
names that ``scanner`` and ``phash_scanner`` bound via ``from db_cache import
cache_db``. Without that substitution scanner tests silently write into the
shared cache of the isolated data dir (cross-test pollution).
"""
import db_cache
import phash_scanner
import scanner


class TestFreshCacheDbPatchesScannerSingletons:
    def test_scanner_cache_db_is_fresh_instance(self, fresh_cache_db):
        # Arrange/Act: fresh_cache_db fixture under test
        # Assert: scanner's module global points at the fresh instance
        assert scanner.cache_db is fresh_cache_db

    def test_phash_scanner_cache_db_is_fresh_instance(self, fresh_cache_db):
        # Arrange/Act: fresh_cache_db fixture under test
        # Assert: phash_scanner's module global points at the fresh instance
        assert phash_scanner.cache_db is fresh_cache_db

    def test_module_cache_db_restored_after_fixture(self):
        # Runs after the two fixture tests above (pytest keeps definition
        # order): the patched module names must be back at the shared
        # module-level singleton — no leaked substitution between tests.
        assert scanner.cache_db is db_cache.cache_db
        assert phash_scanner.cache_db is db_cache.cache_db
