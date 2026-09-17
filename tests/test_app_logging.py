import logging
import os
from logging.handlers import RotatingFileHandler

import app_logging


def _file_handlers():
    root = logging.getLogger()
    return [h for h in root.handlers if isinstance(h, RotatingFileHandler)]


class TestSetupLogging:
    def _restore_root(self, saved_handlers, saved_level):
        root = logging.getLogger()
        root.handlers = saved_handlers
        root.setLevel(saved_level)

    def test_log_file_created_and_written_in_data_dir(self, tmp_path):
        root = logging.getLogger()
        saved_handlers, saved_level = list(root.handlers), root.level
        try:
            log_path = app_logging.setup_logging(str(tmp_path))
            assert log_path == os.path.join(str(tmp_path), app_logging.LOG_FILE_NAME)

            app_logging.get_logger("test_app_logging").warning("hello from the audit")
            for h in _file_handlers():
                h.flush()

            assert os.path.exists(log_path)
            with open(log_path, encoding="utf-8") as f:
                content = f.read()
            assert "hello from the audit" in content
            assert "test_app_logging" in content
        finally:
            self._restore_root(saved_handlers, saved_level)

    def test_double_setup_does_not_duplicate_handlers(self, tmp_path):
        root = logging.getLogger()
        saved_handlers, saved_level = list(root.handlers), root.level
        try:
            app_logging.setup_logging(str(tmp_path))
            first = len(_file_handlers())
            app_logging.setup_logging(str(tmp_path))
            second = len(_file_handlers())
            assert first == 1
            assert second == 1
        finally:
            self._restore_root(saved_handlers, saved_level)

    def test_unwritable_location_never_raises(self, tmp_path):
        root = logging.getLogger()
        saved_handlers, saved_level = list(root.handlers), root.level
        before = len(_file_handlers())
        try:
            # A regular file used as the "data directory": creating the log
            # file inside it must fail — the app has to survive that.
            blocker = tmp_path / "blocker.txt"
            blocker.write_text("not a directory")
            log_path = app_logging.setup_logging(str(blocker))
            assert log_path  # returns the intended path, does not raise
            # The handler must NOT have been attached, and logging still works.
            assert len(_file_handlers()) == before
            app_logging.get_logger("t").warning("still safe")
        finally:
            self._restore_root(saved_handlers, saved_level)

    def test_logger_works_before_setup(self):
        # Modules import get_logger at import time, long before main() runs
        # setup_logging — that must be safe and must NOT attach file handlers.
        before = _file_handlers()
        log = app_logging.get_logger("pre_setup")
        assert isinstance(log, logging.Logger)
        log.debug("silent before setup")
        assert _file_handlers() == before  # no handler materialized
