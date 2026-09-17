import json
import os
import shutil
import threading
import time
import uuid
from typing import List, Optional, Tuple

from db_cache import get_data_dir
from app_logging import get_logger

logger = get_logger(__name__)

OPS_LOG_FILE = os.path.join(get_data_dir(), "operations_log.json")
MAX_OPERATIONS = 100

# Read-modify-write cycles (load -> mutate -> save) run from several worker
# threads at once (delete worker + sweeper worker); without a lock their
# interleaved saves silently dropped whole operations. RLock so public
# load/save stay callable inside the locked compound sections.
_lock = threading.RLock()


def _load_unlocked() -> List[dict]:
    try:
        if os.path.exists(OPS_LOG_FILE):
            with open(OPS_LOG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    return data
    except Exception as ex:
        logger.warning("operations log unreadable: %s", ex)
    return []


def _save_unlocked(ops: List[dict]) -> None:
    """Atomic write (temp file + os.replace): a crash mid-save must never
    truncate the journal — the undo history is the user's safety net."""
    tmp_path = OPS_LOG_FILE + ".tmp"
    try:
        os.makedirs(os.path.dirname(OPS_LOG_FILE), exist_ok=True)
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(ops[:MAX_OPERATIONS], f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, OPS_LOG_FILE)
    except Exception as ex:
        logger.warning("operations log write failed: %s", ex)
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def load_operations() -> List[dict]:
    with _lock:
        return _load_unlocked()


def save_operations(ops: List[dict]) -> None:
    with _lock:
        _save_unlocked(ops)


def append_operation(op_type: str, paths: List[str], freed_bytes: int, details: Optional[dict] = None) -> dict:
    with _lock:
        ops = _load_unlocked()
        op = {
            # Random id: timestamp-ms ids collide when two operations land in the
            # same millisecond, making undo act on the wrong operation (L4).
            "id": uuid.uuid4().hex,
            "type": op_type,
            "ts": time.time(),
            "paths": paths,
            "freed_bytes": freed_bytes,
            "details": details or {},
            "undone": False,
        }
        ops.insert(0, op)
        _save_unlocked(ops)
    return op


def log_delete_operation(deleted_paths: List[str], freed_bytes: int, use_trash: bool) -> dict:
    return append_operation("delete", deleted_paths, freed_bytes, {"trash": use_trash})


def log_hardlink_operation(pairs: List[Tuple[str, str]], freed_bytes: int) -> dict:
    paths = [dup for _, dup in pairs]
    return append_operation("hardlink", paths, freed_bytes, {"pairs": [[o, d] for o, d in pairs]})


def undo_hardlink_operation(op_id: str) -> Tuple[int, List[str]]:
    """Restores separate file copies for a hardlink operation. Returns (restored_count, errors)."""
    with _lock:
        ops = _load_unlocked()
        op = next((o for o in ops if o.get("id") == op_id), None)
        if not op or op.get("type") != "hardlink" or op.get("undone"):
            return 0, ["Operation not found or already undone"]

        restored = 0
        errors: List[str] = []
        for original, duplicate in op.get("details", {}).get("pairs", []):
            try:
                if not os.path.exists(original):
                    errors.append(f"Original missing: {original}")
                    continue
                if os.path.exists(duplicate):
                    if os.path.samefile(original, duplicate):
                        # Copy the original to a temp sibling first and swap it in
                        # atomically: a failed copy must never leave the duplicate
                        # destroyed (M7 — the old code removed it before copying).
                        tmp_copy = duplicate + f".tmp_undo_{os.getpid()}"
                        try:
                            shutil.copy2(original, tmp_copy)
                            os.replace(tmp_copy, duplicate)
                        finally:
                            if os.path.exists(tmp_copy):
                                try:
                                    os.remove(tmp_copy)
                                except OSError:
                                    pass
                    else:
                        # Path was recreated by the user afterwards - leave it untouched.
                        continue
                else:
                    shutil.copy2(original, duplicate)
                restored += 1
            except Exception as ex:
                errors.append(f"{duplicate}: {ex}")

        op["undone"] = True
        _save_unlocked(ops)
        return restored, errors
