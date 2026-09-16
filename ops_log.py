import json
import os
import shutil
import time
from typing import List, Optional, Tuple

from db_cache import get_data_dir

OPS_LOG_FILE = os.path.join(get_data_dir(), "operations_log.json")
MAX_OPERATIONS = 100


def load_operations() -> List[dict]:
    try:
        if os.path.exists(OPS_LOG_FILE):
            with open(OPS_LOG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    return data
    except Exception:
        pass
    return []


def save_operations(ops: List[dict]) -> None:
    try:
        os.makedirs(os.path.dirname(OPS_LOG_FILE), exist_ok=True)
        with open(OPS_LOG_FILE, "w", encoding="utf-8") as f:
            json.dump(ops[:MAX_OPERATIONS], f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def append_operation(op_type: str, paths: List[str], freed_bytes: int, details: Optional[dict] = None) -> dict:
    ops = load_operations()
    op = {
        "id": str(int(time.time() * 1000)),
        "type": op_type,
        "ts": time.time(),
        "paths": paths,
        "freed_bytes": freed_bytes,
        "details": details or {},
        "undone": False,
    }
    ops.insert(0, op)
    save_operations(ops)
    return op


def log_delete_operation(deleted_paths: List[str], freed_bytes: int, use_trash: bool) -> dict:
    return append_operation("delete", deleted_paths, freed_bytes, {"trash": use_trash})


def log_hardlink_operation(pairs: List[Tuple[str, str]], freed_bytes: int) -> dict:
    paths = [dup for _, dup in pairs]
    return append_operation("hardlink", paths, freed_bytes, {"pairs": [[o, d] for o, d in pairs]})


def undo_hardlink_operation(op_id: str) -> Tuple[int, List[str]]:
    """Restores separate file copies for a hardlink operation. Returns (restored_count, errors)."""
    ops = load_operations()
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
                    os.remove(duplicate)
                else:
                    # Path was recreated by the user afterwards - leave it untouched.
                    continue
            shutil.copy2(original, duplicate)
            restored += 1
        except Exception as ex:
            errors.append(f"{duplicate}: {ex}")

    op["undone"] = True
    save_operations(ops)
    return restored, errors
