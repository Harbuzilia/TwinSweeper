"""Test suite bootstrap.

Data-safety contract (user requirement): NO test may touch, scan or delete real
user data. Everything runs inside throwaway temp directories:

* ``DUPLICATER_DATA_DIR`` is pointed at a temp dir BEFORE any project module is
  imported, so ``scan_cache.db`` / ``operations_log.json`` / ``scan_history.json``
  all resolve inside it and never to the real ones.
* Tests only ever scan/modify files they created themselves in ``tmp_path``.
* The isolated dir is removed on interpreter exit (pytest writes only its own
  throwaway files there — never user data).
"""

import atexit
import os
import shutil
import sys
import tempfile

# --- MUST run before the first import of any project module ----------------
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

_ISOLATED_DATA_DIR = tempfile.mkdtemp(prefix="duplicater_test_data_")
os.environ["DUPLICATER_DATA_DIR"] = _ISOLATED_DATA_DIR


@atexit.register
def _cleanup_isolated_data_dir():
    shutil.rmtree(_ISOLATED_DATA_DIR, ignore_errors=True)
# ---------------------------------------------------------------------------


import pytest  # noqa: E402


@pytest.fixture
def isolated_data_dir():
    """The throwaway data directory every project module resolves to."""
    return _ISOLATED_DATA_DIR


@pytest.fixture
def fresh_cache_db(tmp_path):
    """A per-test ScanCacheDB instance with its own temp database.

    The module-level singleton (used by scanner/phash_scanner via ``cache_db``)
    is saved and restored afterwards so other tests are unaffected.
    """
    from db_cache import ScanCacheDB

    saved = ScanCacheDB._instance
    ScanCacheDB._instance = None
    db = ScanCacheDB(str(tmp_path / "scan_cache.db"))
    try:
        yield db
    finally:
        conn = getattr(db._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
            db._local.conn = None
        ScanCacheDB._instance = saved


@pytest.fixture
def isolated_ops_log(tmp_path, monkeypatch):
    """Redirects ops_log storage into the test's tmp_path."""
    import ops_log

    monkeypatch.setattr(ops_log, "OPS_LOG_FILE", str(tmp_path / "operations_log.json"))
    return ops_log


@pytest.fixture
def dup_tree(tmp_path):
    """Deterministic duplicate-file tree:

    tree/
        a.txt          'unique alpha'   (unique)
        other.log      'log line'       (unique)
        dir1/dup.bin   b'X' * 1024      ┐
        dir2/dup.bin   b'X' * 1024      ├ duplicates by content
        dir3/dup.bin   b'X' * 1024      ┘
    """
    root = tmp_path / "tree"
    root.mkdir()
    (root / "a.txt").write_text("unique alpha", encoding="utf-8")
    (root / "other.log").write_text("log line", encoding="utf-8")
    for sub in ("dir1", "dir2", "dir3"):
        d = root / sub
        d.mkdir()
        (d / "dup.bin").write_bytes(b"X" * 1024)
    return root
