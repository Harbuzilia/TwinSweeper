"""Central logging for Duplicater.

`setup_logging()` is called once from main() and attaches a rotating file
handler inside the app's data directory (the same place as scan_cache.db —
DUPLICATER_DATA_DIR redirects it in tests). Every other module only uses
`get_logger(__name__)`, which is safe to call before setup: such loggers
simply propagate to the root logger and, with no handlers, emit nothing
below WARNING to stderr.

The module imports nothing from the project, so any module (db_cache
included) can import it without circular-import risk.
"""

import logging
import os
from logging.handlers import RotatingFileHandler

LOG_FILE_NAME = "duplicater.log"
MAX_BYTES = 1_000_000  # ~1 MB before rotation
BACKUP_COUNT = 2


def get_logger(name: str) -> logging.Logger:
    """Logger for the given module name; works with or without setup_logging."""
    return logging.getLogger(name)


def setup_logging(data_dir: str, level: int = logging.INFO) -> str:
    """Attach a rotating log file in *data_dir*; returns its path.

    Deliberately never raises: if the log file cannot be created (read-only
    directory, locked file, …) the app keeps running without file logging.
    Calling it twice will not duplicate handlers.
    """
    log_path = os.path.join(data_dir, LOG_FILE_NAME)
    root = logging.getLogger()
    root.setLevel(level)

    already_attached = any(
        isinstance(h, RotatingFileHandler) and getattr(h, "baseFilename", "") == os.path.abspath(log_path)
        for h in root.handlers
    )
    if not already_attached:
        try:
            handler = RotatingFileHandler(log_path, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8")
            handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s [%(name)s] %(message)s"))
            root.addHandler(handler)
        except OSError:
            # File logging is a convenience, not a requirement.
            pass

    return log_path
