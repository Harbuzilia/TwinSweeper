"""Priority folders ("keep files from here, clean the rest").

A classic Duplicate Cleaner / AllDup feature for multi-drive cleanup: mark
the canonical locations (e.g. D:\\Photos) as priority — the "priority" smart
selection rule then keeps their copies and marks the others for removal.
"""

import json
import os
import threading

from db_cache import get_data_dir
from app_logging import get_logger

logger = get_logger(__name__)

PRIORITIES_FILE = os.path.join(get_data_dir(), "folder_priorities.json")
_lock = threading.RLock()


def load_priority_folders() -> list:
    with _lock:
        try:
            if os.path.exists(PRIORITIES_FILE):
                with open(PRIORITIES_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, list):
                        return [str(p) for p in data if str(p).strip()]
        except Exception as ex:
            logger.warning("folder priorities unreadable: %s", ex)
        return []


def save_priority_folders(folders: list) -> None:
    with _lock:
        cleaned = sorted({os.path.normpath(str(f)) for f in folders if str(f).strip()})
        tmp_path = PRIORITIES_FILE + ".tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(cleaned, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, PRIORITIES_FILE)
        except Exception as ex:
            logger.warning("folder priorities write failed: %s", ex)


def is_priority_path(path: str, priorities: list) -> bool:
    """True when *path* lives under one of the priority folders
    (component-wise; Windows paths are case-insensitive)."""
    p_norm = os.path.normcase(os.path.normpath(path))
    for pref in priorities:
        r_norm = os.path.normcase(os.path.normpath(str(pref)))
        if not r_norm:
            continue
        if p_norm == r_norm or p_norm.startswith(r_norm + os.sep):
            return True
    return False
