import flet as ft
import os
import shutil
import sys
import json
import threading
from datetime import datetime
from typing import List, Dict, Tuple

try:
    import winreg
    HAS_WINREG = True
except ImportError:
    HAS_WINREG = False

from scanner import (
    scan_directory, scan_for_sample, compare_folders, FileInfo, format_file_size, compute_wasted_bytes,
    verify_file_unchanged
)
from phash_scanner import scan_similar_images
from hardlink_manager import batch_replace_with_hardlinks, is_same_volume
from sweeper import remove_emptied_parents
from db_cache import cache_db
from ops_log import load_operations, log_delete_operation, log_hardlink_operation, log_move_operation, undo_hardlink_operation, undo_move_operation, get_data_dir

from ui.search_view import SearchView
from ui.results_view import ResultsView
from ui.sample_search_view import SampleSearchView
from ui.compare_view import CompareView
from ui.sweeper_view import SweeperView
from ui.components import (
    get_styled_card, get_stat_card, get_primary_button, get_outlined_button, get_header_row, get_badge,
    get_styled_dialog, get_action_icon_button, tint,
    set_active_theme, get_active_theme_key, get_current_theme,
    BG_COLOR, SURFACE_HOVER, BORDER_COLOR,
    PRIMARY_COLOR, ACCENT_COLOR, SUCCESS_COLOR, WARNING_COLOR, DANGER_COLOR,
    TEXT_PRIMARY, TEXT_SECONDARY, TEXT_MUTED
)
from locales import get_text, set_current_language
from app_logging import setup_logging, get_logger
from app_info import APP_NAME, APP_TAGLINE, APP_VERSION

logger = get_logger(__name__)

try:
    from send2trash import send2trash
    HAS_SEND2TRASH = True
except ImportError:
    HAS_SEND2TRASH = False

HISTORY_FILE = os.path.join(get_data_dir(), "scan_history.json")

# Scan history is written from worker threads while other operations append
# too — same locked read-modify-write discipline as the operations journal.
_history_lock = threading.RLock()

SETTINGS_FILE = os.path.join(get_data_dir(), "settings.json")

DEFAULT_SETTINGS = {"language": "ru", "theme": "dark_slate", "trash_default": True}
_settings_lock = threading.RLock()


def load_settings() -> dict:
    """Persisted user settings (language / theme / trash-by-default). Written
    from the UI thread; unknown or missing keys fall back to the defaults."""
    with _settings_lock:
        try:
            if os.path.exists(SETTINGS_FILE):
                with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        merged = dict(DEFAULT_SETTINGS)
                        merged.update(data)
                        return merged
        except Exception as ex:
            logger.warning("settings unreadable: %s", ex)
        return dict(DEFAULT_SETTINGS)


def save_settings(updates: dict) -> None:
    with _settings_lock:
        settings = load_settings()
        settings.update(updates)
        tmp_path = SETTINGS_FILE + ".tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(settings, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, SETTINGS_FILE)
        except Exception as ex:
            logger.warning("settings write failed: %s", ex)

# Schema mirror of scanner.compare_folders output — safe fallback when the
# comparison fails (H2: a mismatched shape used to crash the results UI).
EMPTY_COMPARE_RESULT = {"unique_a": [], "unique_b": [], "common": [], "total_files": 0}

def load_history() -> List[dict]:
    with _history_lock:
        try:
            if os.path.exists(HISTORY_FILE):
                with open(HISTORY_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    # A corrupt/foreign file (e.g. {}) must not crash the scan
                    # worker later with AttributeError on .append (round 3).
                    if isinstance(data, list):
                        return data
        except Exception as ex:
            logger.warning("scan history unreadable: %s", ex)
        return []

def save_history(history: List[dict]):
    # Atomic write: a crash mid-save must not destroy the history file.
    with _history_lock:
        tmp_path = HISTORY_FILE + ".tmp"
        try:
            with open(tmp_path, 'w', encoding='utf-8') as f:
                json.dump(history[-30:], f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, HISTORY_FILE)
        except Exception as ex:
            logger.warning("scan history write failed: %s", ex)

def add_to_history(directories: List[str], duplicates_found: int, wasted_space: int):
    # Locked read-modify-write: the delete worker's journaling and a fresh
    # scan's history append could interleave and drop entries.
    with _history_lock:
        history = load_history()
        history.append({
            "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "folders": directories,
            "duplicates": duplicates_found,
            "wasted": wasted_space
        })
        save_history(history)

# Windows Context Menu Integration Helpers
def is_context_menu_registered() -> bool:
    if not HAS_WINREG:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Classes\Directory\shell\Duplicater"):
            return True
    except OSError:
        return False

def register_context_menu(app_title: str = "Найти дубликаты в Duplicater"):
    if not HAS_WINREG:
        return False, "winreg not available"
    try:
        if getattr(sys, 'frozen', False):
            cmd = f'"{sys.executable}" "%1"'
            bg_cmd = f'"{sys.executable}" "%V"'
        else:
            main_py = os.path.abspath(os.path.join(os.path.dirname(__file__), "main.py"))
            python_exe = sys.executable.replace("python.exe", "pythonw.exe") if os.path.exists(sys.executable.replace("python.exe", "pythonw.exe")) else sys.executable
            cmd = f'"{python_exe}" "{main_py}" "%1"'
            bg_cmd = f'"{python_exe}" "{main_py}" "%V"'

        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Classes\Directory\shell\Duplicater") as key:
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, app_title)
            winreg.SetValueEx(key, "Icon", 0, winreg.REG_SZ, sys.executable)
            with winreg.CreateKey(key, "command") as cmd_key:
                winreg.SetValueEx(cmd_key, "", 0, winreg.REG_SZ, cmd)

        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Classes\Directory\Background\shell\Duplicater") as key:
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, app_title)
            winreg.SetValueEx(key, "Icon", 0, winreg.REG_SZ, sys.executable)
            with winreg.CreateKey(key, "command") as cmd_key:
                winreg.SetValueEx(cmd_key, "", 0, winreg.REG_SZ, bg_cmd)
        return True, ""
    except Exception as ex:
        return False, str(ex)

def unregister_context_menu():
    if not HAS_WINREG:
        return False, "winreg not available"
    try:
        for subkey in [
            r"Software\Classes\Directory\shell\Duplicater\command",
            r"Software\Classes\Directory\shell\Duplicater",
            r"Software\Classes\Directory\Background\shell\Duplicater\command",
            r"Software\Classes\Directory\Background\shell\Duplicater"
        ]:
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, subkey)
            except OSError:
                pass
        return True, ""
    except Exception as ex:
        return False, str(ex)

def build_hardlink_log_pairs(
    groups_map: Dict[str, List[Tuple[str, int, float]]], succeeded_paths: List[str]
) -> List[Tuple[str, str]]:
    """(original, duplicate_path) pairs for the operations journal — only files
    that were actually hardlinked.

    groups_map values hold (path, size, mtime) snapshots from ResultsView (C2),
    while batch_replace_with_hardlinks returns plain path strings. The two
    shapes must be reconciled explicitly: comparing a tuple against a string
    never matches, which once left hardlink operations unjournaled — and
    hardlink undo dead — despite the linking itself succeeding."""
    succeeded = set(succeeded_paths)
    return [
        (original, entry[0])
        for original, entries in groups_map.items()
        for entry in entries
        if entry[0] in succeeded
    ]

def _reserve_destination(destination: str, source_path: str) -> str:
    """Atomically reserve a collision-free destination path via O_CREAT|O_EXCL.

    An existence-check-then-write has a TOCTOU window where a concurrent
    process could create the file and os.replace would then silently overwrite
    it. Reserving with O_EXCL closes that window. The call leaves an empty
    placeholder that the caller overwrites on success or removes on failure."""
    name = os.path.basename(source_path)
    stem, ext = os.path.splitext(name)
    candidate = os.path.join(destination, name)
    n = 1
    while True:
        try:
            fd = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            return candidate
        except FileExistsError:
            candidate = os.path.join(destination, f"{stem} ({n}){ext}")
            n += 1

# --- Operation pipelines (module-level: testable without the GUI) -----------
# Every worker in main(page) is a thin UI shell around one of these: verify →
# act → journal, in 200-file chunks, cancellable between chunks.

PIPELINE_CHUNK = 200

def perform_delete(file_entries: List[Tuple[str, int, float]], use_trash: bool, cancel_flag: List[bool] = None, progress_callback=None, remove_empty_folders: bool = False) -> dict:
    """The delete pipeline. file_entries: (path, size, mtime) scan snapshots.

    Per file: C2 re-verification, then Recycle Bin (a trash failure NEVER
    falls back to a permanent delete — H5) or os.remove. Journaled at the end
    with only the actually deleted paths. progress_callback(processed, total)
    fires once per 200-file chunk. With remove_empty_folders=True, folders
    left empty by the deletions are removed too (os.rmdir only — a folder
    that still holds anything is always left alone)."""
    total = len(file_entries)
    state = {"deleted_count": 0, "total_freed": 0, "errors": [], "actually_deleted": [], "empty_folders_removed": 0}

    try:
        for start in range(0, total, PIPELINE_CHUNK):
            if cancel_flag and cancel_flag[0]:
                return state
            for path, expected_size, expected_mtime in file_entries[start:start + PIPELINE_CHUNK]:
                # C2: only delete the file that was actually scanned.
                ok, reason = verify_file_unchanged(path, expected_size, expected_mtime)
                if not ok:
                    state["errors"].append(get_text("error_verify_failed").format(path, reason))
                    continue
                try:
                    if use_trash:
                        if not HAS_SEND2TRASH:
                            # The user asked for the Recycle Bin but the library
                            # is unavailable — silently deleting forever would
                            # break the core safety promise (H5). Refuse.
                            state["errors"].append(get_text("trash_unavailable").format(path))
                            continue
                        try:
                            send2trash(path)
                        except Exception as trash_ex:
                            # H5: a failed move to the Recycle Bin must never
                            # silently become an unrecoverable delete.
                            state["errors"].append(get_text("trash_failed").format(path, trash_ex))
                            continue
                    else:
                        os.remove(path)
                    state["deleted_count"] += 1
                    state["total_freed"] += expected_size
                    state["actually_deleted"].append(path)
                except Exception as ex:
                    state["errors"].append(get_text("error_delete").format(path, ex))

            if progress_callback:
                progress_callback(min(start + PIPELINE_CHUNK, total), total)
    finally:
        # Journal even on a mid-operation cancel/early-return: files already
        # deleted MUST stay recoverable via undo (round 3 — a cancel used to
        # skip the journal, making those deletions permanent).
        if state["actually_deleted"]:
            log_delete_operation(state["actually_deleted"], state["total_freed"], use_trash)
            if remove_empty_folders:
                removed, errs = remove_emptied_parents(state["actually_deleted"])
                state["empty_folders_removed"] = removed
                state["errors"].extend(errs)
    return state

def perform_hardlink(groups_map: Dict[str, List[Tuple[str, int, float]]], cancel_flag: List[bool] = None, progress_callback=None) -> dict:
    """The hardlink pipeline. groups_map: {original: [(dupe, size, mtime)]}.

    Per duplicate: C2 re-verification, then batch_replace_with_hardlinks
    (which itself refuses non-identical content — C1). Only actually linked
    files are journaled, so undo can never try to restore a file that was
    never linked."""
    total_dupes = sum(len(dups) for dups in groups_map.values())
    state = {"success_count": 0, "freed_bytes": 0, "errors": [], "succeeded_paths": [], "total": total_dupes}
    items = [(orig, dup) for orig, dups in groups_map.items() for dup in dups]
    processed = 0

    try:
        for start in range(0, len(items), PIPELINE_CHUNK):
            if cancel_flag and cancel_flag[0]:
                return state
            chunk = items[start:start + PIPELINE_CHUNK]
            chunk_map = {}
            for orig, dup_entry in chunk:
                path, expected_size, expected_mtime = dup_entry
                # C2: only link the file that was actually scanned.
                ok, reason = verify_file_unchanged(path, expected_size, expected_mtime)
                if not ok:
                    state["errors"].append(get_text("error_verify_failed").format(path, reason))
                    continue
                chunk_map.setdefault(orig, []).append(path)
            if chunk_map:
                sc, fb, errs, sp = batch_replace_with_hardlinks(chunk_map)
                state["success_count"] += sc
                state["freed_bytes"] += fb
                state["errors"].extend(errs)
                state["succeeded_paths"].extend(sp)
            processed += len(chunk)
            if progress_callback:
                progress_callback(processed, total_dupes)
    finally:
        # Journal even on a mid-operation cancel: links already created must
        # stay undoable (round 3).
        if state["succeeded_paths"]:
            pairs = build_hardlink_log_pairs(groups_map, state["succeeded_paths"])
            if pairs:
                log_hardlink_operation(pairs, state["freed_bytes"])
    return state

def perform_move(file_entries: List[Tuple[str, int, float]], destination: str, cancel_flag: List[bool] = None, progress_callback=None, remove_empty_folders: bool = False) -> dict:
    """The move pipeline — the reversible alternative to deletion.

    Per file: C2 re-verification, an atomically reserved collision-free
    destination name, then either an atomic same-volume rename (os.replace)
    or a cross-volume copy-to-temp + replace. The source is dropped only once
    the copy is fully in place; if dropping it fails, the copy is removed too
    so we never leave an invisible duplicate. Journaled for undo (even on a
    mid-operation cancel). With remove_empty_folders=True, source folders
    left empty by the moves are removed too (os.rmdir only)."""
    total = len(file_entries)
    state = {"moved_count": 0, "actually_moved": [], "errors": [], "destination": destination, "empty_folders_removed": 0}
    try:
        os.makedirs(destination, exist_ok=True)
    except OSError as ex:
        state["errors"].append(get_text("error_move").format(destination, ex))
        return state

    try:
        for start in range(0, total, PIPELINE_CHUNK):
            if cancel_flag and cancel_flag[0]:
                return state
            for path, expected_size, expected_mtime in file_entries[start:start + PIPELINE_CHUNK]:
                ok, reason = verify_file_unchanged(path, expected_size, expected_mtime)
                if not ok:
                    state["errors"].append(get_text("error_verify_failed").format(path, reason))
                    continue
                # The reservation itself can fail (read-only destination,
                # disk full): report it as a per-file error and keep the
                # pipeline running for the remaining files — an escape here
                # used to kill the whole worker thread (2.2b).
                try:
                    dest_path = _reserve_destination(destination, path)
                except Exception as ex:
                    state["errors"].append(get_text("error_move").format(path, ex))
                    continue
                try:
                    if is_same_volume(path, destination):
                        # Same volume: atomic rename, no copy window at all.
                        os.replace(path, dest_path)
                    else:
                        tmp_copy = dest_path + f".tmp_move_{os.getpid()}"
                        try:
                            shutil.copy2(path, tmp_copy)
                            os.replace(tmp_copy, dest_path)
                        finally:
                            if os.path.exists(tmp_copy):
                                try:
                                    os.remove(tmp_copy)
                                except OSError:
                                    pass
                        # The copy is in place; now drop the source. If that
                        # fails (locked file), remove the copy — never leave an
                        # invisible duplicate behind.
                        try:
                            os.remove(path)
                        except OSError:
                            try:
                                os.remove(dest_path)
                            except OSError:
                                pass
                            raise
                    state["moved_count"] += 1
                    state["actually_moved"].append((path, dest_path))
                except Exception as ex:
                    state["errors"].append(get_text("error_move").format(path, ex))
                    # Remove the reserved placeholder (or any partial) on failure.
                    if os.path.exists(dest_path):
                        try:
                            os.remove(dest_path)
                        except OSError:
                            pass

            if progress_callback:
                progress_callback(min(start + PIPELINE_CHUNK, total), total)
    finally:
        if state["actually_moved"]:
            log_move_operation(state["actually_moved"], destination)
            if remove_empty_folders:
                removed, errs = remove_emptied_parents([src for src, _ in state["actually_moved"]])
                state["empty_folders_removed"] = removed
                state["errors"].extend(errs)
    return state

def main(page: ft.Page):
    # File log lives next to scan_cache.db (DUPLICATER_DATA_DIR redirects it in tests).
    setup_logging(get_data_dir())
    logger.info("Duplicater %s started (log file initialized)", APP_VERSION)

    # Persisted settings (round 2: language/theme/trash default used to reset
    # to hardcoded values on every launch).
    app_settings = load_settings()
    current_language = app_settings.get("language", "ru")
    set_current_language(current_language)  # core modules localize progress too
    trash_default = [bool(app_settings.get("trash_default", True))]

    # App Window & Appearance
    page.title = get_text("app_title", current_language)
    page.theme_mode = ft.ThemeMode.LIGHT if app_settings.get("theme") == "light_clean" else ft.ThemeMode.DARK
    page.bgcolor = BG_COLOR
    page.padding = 0
    page.theme = ft.Theme(
        font_family="Segoe UI",
        scrollbar_theme=ft.ScrollbarTheme(
            thumb_visibility=True,
            thickness=6,
            radius=4,
        )
    )
    saved_theme = app_settings.get("theme")
    if saved_theme:
        set_active_theme(saved_theme)
        page.bgcolor = get_current_theme()["BG_COLOR"]
    current_nav_index = 0

    # True while the results screen (not a nav tab) is displayed — a language
    # or theme switch must not destroy the user's open results.
    showing_results = [False]
    # Live tab views: switching tabs used to recreate every view, wiping the
    # sweeper results, compare state and search filters each time.
    view_cache: Dict[int, ft.Control] = {}

    def set_trash_default(value: bool):
        trash_default[0] = bool(value)
        save_settings({"trash_default": bool(value)})
        # Cached views captured the old value at construction — drop them so the
        # sweeper is rebuilt with the new trash default on the next visit
        # (round 3: the setting used to have no effect until a language/theme
        # switch happened to clear the cache).
        view_cache.clear()

    # CLI Directory Arguments
    initial_cli_dirs = []
    for arg in sys.argv[1:]:
        if os.path.exists(arg) and os.path.isdir(arg):
            initial_cli_dirs.append(os.path.abspath(arg))

    search_view_instance = None
    results_view_instance = None

    def set_language(lang: str):
        nonlocal current_language
        current_language = lang
        save_settings({"language": lang})
        set_current_language(lang)
        page.title = get_text("app_title", lang)
        # Cached views carry their build-time language — rebuild them lazily.
        view_cache.clear()
        if showing_results[0]:
            # Keep the open results: rebuilding the current tab would silently
            # discard the user's selection and filters. The sidebar still
            # re-renders in the new language.
            render_app_shell()
            page.update()
            return
        on_nav_change(current_nav_index)

    def change_theme(theme_name: str):
        set_active_theme(theme_name)
        save_settings({"theme": theme_name})
        theme_colors = get_current_theme()
        page.bgcolor = theme_colors["BG_COLOR"]
        page.theme_mode = ft.ThemeMode.LIGHT if theme_name == "light_clean" else ft.ThemeMode.DARK
        page.theme = ft.Theme(
            font_family="Segoe UI",
            scrollbar_theme=ft.ScrollbarTheme(
                thumb_visibility=True,
                thickness=6,
                radius=4,
            )
        )
        view_cache.clear()
        if showing_results[0]:
            # Same reasoning as set_language: results survive a theme switch.
            render_app_shell()
            page.update()
            return
        on_nav_change(current_nav_index)

    # Scan Runners
    def run_scan(
        directories: List[str],
        by_name: bool,
        by_size: bool,
        by_hash: bool,
        by_byte: bool,
        progress_callback,
        on_cancel_setup=None,
        exclude_patterns=None,
        turbo_mode=True,
        min_size_bytes=0,
        max_size_bytes=None,
        ignore_empty_files=True,
        on_scan_finished=None,
        is_phash=False,
        phash_threshold=0.90
    ):
        # Per-run cancel token: the previously shared flag let a newly started
        # scan reset (or cancel) another still-running one.
        run_cancel = [False]

        def cancel_this_run():
            run_cancel[0] = True

        if on_cancel_setup:
            on_cancel_setup(cancel_this_run)

        def _worker():
            results = {}
            had_error = False
            try:
                if is_phash:
                    results = scan_similar_images(
                        directories=directories,
                        similarity_threshold=phash_threshold,
                        progress_callback=progress_callback,
                        cancel_flag=run_cancel,
                        exclude_patterns=exclude_patterns
                    )
                else:
                    results = scan_directory(
                        directories=directories,
                        by_name=by_name,
                        by_size=by_size,
                        by_hash=by_hash,
                        by_byte=by_byte,
                        progress_callback=progress_callback,
                        cancel_flag=run_cancel,
                        exclude_patterns=exclude_patterns,
                        turbo_mode=turbo_mode,
                        min_size_bytes=min_size_bytes,
                        max_size_bytes=max_size_bytes,
                        ignore_empty_files=ignore_empty_files
                    )
            except Exception as ex:
                had_error = True
                progress_callback(get_text("scan_error").format(ex), None)
            finally:
                if on_scan_finished:
                    on_scan_finished()
                # 2.2d: release this worker thread's sqlite connection —
                # per-thread handles used to live until interpreter GC.
                cache_db.close()

            if run_cancel[0]:
                progress_callback(get_text("scan_cancelled", current_language), None)
                return
            # M8: a failed scan must not be reported as "no duplicates found".
            if had_error:
                return

            if not results:
                progress_callback(get_text("no_duplicates", current_language), None)
            else:
                total_dupes = sum(max(0, len(files) - 1) for files in results.values())
                total_wasted = compute_wasted_bytes(results)
                add_to_history(directories, total_dupes, total_wasted)
                # Content is only verified identical when hashing/byte-compare
                # ran; pHash results are perceptually similar, not identical.
                show_results_screen(
                    results,
                    allow_hardlink=not is_phash,
                    content_verified=(not is_phash) and (by_hash or by_byte)
                )

        # page.run_thread keeps the flet page context in the worker — the
        # ResultsView FilePicker constructed there then auto-registers (H3).
        page.run_thread(_worker)

    def run_sample_scan(
        sample_path: str,
        directories: List[str],
        by_name: bool,
        by_size: bool,
        by_hash: bool,
        by_byte: bool,
        progress_callback,
        on_cancel_setup=None,
        on_scan_finished=None
    ):
        # Per-run cancel token (same reasoning as run_scan).
        run_cancel = [False]

        def cancel_this_run():
            run_cancel[0] = True

        if on_cancel_setup:
            on_cancel_setup(cancel_this_run)

        def _worker():
            found_files = []
            had_error = False
            try:
                found_files = scan_for_sample(
                    sample_path=sample_path,
                    search_directories=directories,
                    by_name=by_name,
                    by_size=by_size,
                    by_hash=by_hash,
                    by_byte=by_byte,
                    progress_callback=progress_callback,
                    cancel_flag=run_cancel
                )
            except Exception as ex:
                had_error = True
                progress_callback(get_text("scan_error").format(ex), None)
            finally:
                if on_scan_finished:
                    on_scan_finished()
                # 2.2d: release this worker thread's sqlite connection (see
                # run_scan above).
                cache_db.close()

            if run_cancel[0]:
                progress_callback(get_text("scan_cancelled", current_language), None)
                return
            if had_error:
                return

            if found_files:
                try:
                    s_stat = os.stat(sample_path)
                    sample_info = FileInfo(sample_path, os.path.basename(sample_path), s_stat.st_size, s_stat.st_ctime, s_stat.st_mtime)
                    results = {f"Sample: {sample_info.name}": [sample_info] + found_files}
                    show_results_screen(results, content_verified=(by_hash or by_byte))
                except OSError:
                    progress_callback(get_text("no_duplicates", current_language), None)
            else:
                progress_callback(get_text("no_duplicates", current_language), None)

        page.run_thread(_worker)

    def run_compare(folder_a: str, folder_b: str, result_callback, progress_callback, error_callback=None):
        def _worker():
            try:
                results = compare_folders(folder_a, folder_b, progress_callback)
            except Exception as ex:
                # progress_callback takes exactly one message argument — the old
                # two-arg call raised a second TypeError right inside this handler,
                # and the swallowed crash left the progress bar spinning forever.
                msg = get_text("compare_error", current_language).format(ex)
                if error_callback:
                    error_callback(msg)
                else:
                    progress_callback(msg)
                # An error must not be rendered as "folders have nothing in
                # common" — that would invite a wrong user decision (M8 pattern).
                return
            # H2: the fallback must mirror the real compare_folders schema,
            # otherwise the results view dies on a KeyError.
            result_callback(results or EMPTY_COMPARE_RESULT)

        page.run_thread(_worker)

    # View Transition Handlers
    def show_results_screen(results: Dict[str, List[FileInfo]], allow_hardlink: bool = True, content_verified: bool = True):
        nonlocal results_view_instance
        def on_back():
            showing_results[0] = False
            on_nav_change(0)
            if search_view_instance:
                search_view_instance.reset_state()

        results_view_instance = ResultsView(
            results=results,
            on_back=on_back,
            on_delete=delete_files_handler,
            on_hardlink=hardlink_files_handler,
            on_move=move_files_handler,
            language=current_language,
            allow_hardlink=allow_hardlink,
            trash_default=trash_default[0],
            content_verified=content_verified,
            trash_available=HAS_SEND2TRASH
        )
        showing_results[0] = True
        main_content_container.content = results_view_instance
        page.update()

    def delete_files_handler(file_entries: List[Tuple[str, int, float]], use_trash: bool = True, remove_empty_folders: bool = False):
        """Thin UI shell over perform_delete (see the module-level pipeline)."""
        # Capture the view NOW: the worker must not act on a *different*
        # ResultsView if the user pressed Back and rescanned mid-delete.
        target_view = results_view_instance

        def _worker():
            try:
                def report(done, tot):
                    if target_view is not None:
                        target_view.operation_progress.value = done / tot if tot else 1
                        target_view.operation_status.value = get_text("deleting", current_language).format(done, tot)
                        try:
                            page.update()
                        except Exception:
                            pass

                state = perform_delete(file_entries, use_trash, progress_callback=report, remove_empty_folders=remove_empty_folders)

                if target_view is not None:
                    target_view.operation_progress.visible = False
                    target_view.operation_status.visible = False
                    if state["actually_deleted"]:
                        target_view.remove_files(state["actually_deleted"])
            finally:
                # Whatever happened, the action buttons must become clickable again.
                if target_view is not None:
                    target_view._operation_busy = False

            msg = get_text("deleted_count", current_language).format(state["deleted_count"])
            msg += "\n" + get_text("deleted_space_freed", current_language).format(format_file_size(state["total_freed"]))
            if state["empty_folders_removed"]:
                msg += "\n" + get_text("empty_folders_removed_count", current_language).format(state["empty_folders_removed"])
            if state["errors"]:
                msg += "\n\n" + get_text("dlg_errors_list") + ":\n" + "\n".join(state["errors"][:5])

            dlg = get_styled_dialog(
                title=get_text("deletion_complete", current_language),
                title_color=SUCCESS_COLOR,
                content=ft.Text(msg, size=13, color=TEXT_SECONDARY),
                actions=[
                    get_primary_button(text=get_text("ok", current_language), on_click=lambda _: page.pop_dialog(), height=36)
                ]
            )
            page.show_dialog(dlg)
            try:
                page.update()
            except Exception:
                pass

        page.run_thread(_worker)

    def hardlink_files_handler(groups_map: Dict[str, List[Tuple[str, int, float]]]):
        """Thin UI shell over perform_hardlink (see the module-level pipeline)."""
        # Capture the view now — same reasoning as delete_files_handler.
        target_view = results_view_instance

        def _worker():
            try:
                def report(done, tot):
                    if target_view is not None:
                        target_view.operation_progress.value = done / tot if tot else 1
                        target_view.operation_status.value = get_text("hardlinking", current_language).format(done, tot)
                        try:
                            page.update()
                        except Exception:
                            pass

                state = perform_hardlink(groups_map, progress_callback=report)

                if target_view is not None:
                    target_view.operation_progress.visible = False
                    target_view.operation_status.visible = False
                    # Remove only files that were actually replaced; failed ones stay selectable.
                    if state["succeeded_paths"]:
                        target_view.remove_files(state["succeeded_paths"])
            finally:
                # Whatever happened, the action buttons must become clickable again.
                if target_view is not None:
                    target_view._operation_busy = False

            msg = get_text("hardlink_success_msg", current_language).format(state["success_count"])
            msg += "\n" + get_text("hardlink_space_saved", current_language).format(format_file_size(state["freed_bytes"]))
            if state["errors"]:
                msg += "\n\n" + get_text("dlg_warnings_list") + ":\n" + "\n".join(state["errors"][:4])

            dlg = get_styled_dialog(
                title=get_text("hardlink_complete", current_language),
                title_color=PRIMARY_COLOR,
                content=ft.Text(msg, size=13, color=TEXT_SECONDARY),
                actions=[
                    get_primary_button(text=get_text("ok", current_language), on_click=lambda _: page.pop_dialog(), height=36)
                ]
            )
            page.show_dialog(dlg)
            try:
                page.update()
            except Exception:
                pass

        page.run_thread(_worker)

    def move_files_handler(file_entries: List[Tuple[str, int, float]], destination: str, remove_empty_folders: bool = False):
        """Thin UI shell over perform_move — the reversible alternative to
        deletion (round 2 / stage 4c)."""
        target_view = results_view_instance

        def _worker():
            try:
                def report(done, tot):
                    if target_view is not None:
                        target_view.operation_progress.value = done / tot if tot else 1
                        target_view.operation_status.value = get_text("move_success_msg", current_language).format(done)
                        try:
                            page.update()
                        except Exception:
                            pass

                state = perform_move(file_entries, destination, progress_callback=report, remove_empty_folders=remove_empty_folders)

                if target_view is not None:
                    target_view.operation_progress.visible = False
                    target_view.operation_status.visible = False
                    # Sources that actually moved leave the results list.
                    if state["actually_moved"]:
                        target_view.remove_files([src for src, _ in state["actually_moved"]])
            finally:
                if target_view is not None:
                    target_view._operation_busy = False

            msg = get_text("move_success_msg", current_language).format(state["moved_count"])
            msg += "\n" + get_text("moved_to", current_language).format(destination)
            if state["empty_folders_removed"]:
                msg += "\n" + get_text("empty_folders_removed_count", current_language).format(state["empty_folders_removed"])
            if state["errors"]:
                msg += "\n\n" + get_text("dlg_errors_list") + ":\n" + "\n".join(state["errors"][:5])

            dlg = get_styled_dialog(
                title=get_text("move_complete", current_language),
                title_color=SUCCESS_COLOR,
                content=ft.Text(msg, size=13, color=TEXT_SECONDARY),
                actions=[
                    get_primary_button(text=get_text("ok", current_language), on_click=lambda _: page.pop_dialog(), height=36)
                ]
            )
            page.show_dialog(dlg)
            try:
                page.update()
            except Exception:
                pass

        page.run_thread(_worker)

    def rescan_history_entry(folders: List[str]):
        on_nav_change(0)
        if search_view_instance:
            search_view_instance.selected_directories = [f for f in folders if os.path.exists(f)]
            search_view_instance.update_folders_list()

    # Sub-views Builders
    def build_history_tab():
        history = load_history()
        if not history:
            return ft.Column([
                get_header_row(
                    title=get_text("history", current_language),
                    subtitle=get_text("history_desc", current_language)
                ),
                ft.Container(
                    content=ft.Column([
                        ft.Icon(ft.Icons.HISTORY_ROUNDED, size=48, color=TEXT_MUTED),
                        ft.Text(get_text("no_history", current_language), size=14, color=TEXT_MUTED),
                    ], alignment=ft.MainAxisAlignment.CENTER, horizontal_alignment=ft.CrossAxisAlignment.CENTER),
                    alignment=ft.Alignment.CENTER,
                    expand=True
                )
            ], spacing=16, expand=True)

        total_analyzed = sum(item.get("wasted", 0) for item in history)
        history_items = []

        def clear_history_action(e):
            save_history([])
            on_nav_change(4)

        for idx, item in enumerate(reversed(history)):
            real_idx = len(history) - 1 - idx
            folders_list = item.get("folders", [])
            wasted_str = format_file_size(item.get("wasted", 0))

            def delete_entry(i=real_idx):
                h = load_history()
                if 0 <= i < len(h):
                    h.pop(i)
                    save_history(h)
                    on_nav_change(4)

            history_items.append(
                get_styled_card(
                    ft.Row([
                        ft.Container(
                            content=ft.Icon(ft.Icons.SCHEDULE_ROUNDED, color=PRIMARY_COLOR, size=20),
                            bgcolor=tint(PRIMARY_COLOR, "18"),
                            border_radius=8,
                            padding=8
                        ),
                        ft.Column([
                            ft.Text(item.get("date", ""), weight=ft.FontWeight.BOLD, size=13, color=TEXT_PRIMARY),
                            ft.Text(", ".join(folders_list[:2]) + ("..." if len(folders_list) > 2 else ""), size=12, color=TEXT_MUTED),
                        ], expand=True, spacing=2),
                        ft.Column([
                            get_badge(f"{item.get('duplicates', 0)} {get_text('files', current_language)}", color=PRIMARY_COLOR),
                            get_badge(f"-{wasted_str}", color=DANGER_COLOR),
                        ], horizontal_alignment=ft.CrossAxisAlignment.END, spacing=4),
                        get_primary_button(
                            text=get_text("rescan_now", current_language),
                            on_click=lambda _, f=folders_list: rescan_history_entry(f),
                            icon=ft.Icons.REFRESH_ROUNDED,
                            height=34
                        ),
                        get_action_icon_button(
                            icon=ft.Icons.DELETE_OUTLINE_ROUNDED,
                            icon_color=DANGER_COLOR,
                            tooltip=get_text("delete", current_language),
                            on_click=lambda _, i=real_idx: delete_entry(i),
                            button_size=34,
                            icon_size=18
                        )
                    ], vertical_alignment=ft.CrossAxisAlignment.CENTER, spacing=12),
                    padding=12
                )
            )

        operations = load_operations()
        ops_items = []

        def undo_operation_action(op: dict):
            undo_fn = undo_move_operation if op.get("type") == "move" else undo_hardlink_operation
            op_id = op.get("id")

            def _worker():
                # M7b: undo copies/moves whole files — must not run on the UI thread.
                restored, undo_errors = undo_fn(op_id)
                undo_msg = get_text("op_undo_done", current_language).format(restored)
                if undo_errors:
                    undo_msg += "\n" + "\n".join(undo_errors[:3])
                undo_dlg = get_styled_dialog(
                    title=get_text("ops_log_title", current_language),
                    title_color=PRIMARY_COLOR,
                    content=ft.Text(undo_msg, size=13, color=TEXT_SECONDARY),
                    actions=[get_primary_button(text=get_text("ok", current_language), on_click=lambda _: page.pop_dialog(), height=36)]
                )
                page.show_dialog(undo_dlg)
                on_nav_change(4)

            page.run_thread(_worker)

        for op in operations[:10]:
            op_type = op.get("type")
            if op_type == "hardlink":
                op_icon, op_color, op_title_key = ft.Icons.LINK_ROUNDED, PRIMARY_COLOR, "op_hardlink"
            elif op_type == "move":
                op_icon, op_color, op_title_key = ft.Icons.DRIVE_FILE_MOVE_OUTLINED, ACCENT_COLOR, "op_move"
            else:
                op_icon, op_color, op_title_key = ft.Icons.DELETE_OUTLINE_ROUNDED, DANGER_COLOR, "op_delete"
            op_time = datetime.fromtimestamp(op.get("ts", 0)).strftime("%Y-%m-%d %H:%M")
            op_row_controls = [
                ft.Container(
                    content=ft.Icon(op_icon, color=op_color, size=16),
                    bgcolor=tint(op_color, "18"),
                    border_radius=6,
                    padding=6
                ),
                ft.Column([
                    ft.Text(get_text(op_title_key, current_language).format(len(op.get("paths", []))), weight=ft.FontWeight.BOLD, size=13, color=TEXT_PRIMARY),
                    ft.Text(f"{op_time}  •  -{format_file_size(op.get('freed_bytes', 0))}", size=11, color=TEXT_MUTED),
                ], expand=True, spacing=2),
            ]
            if op_type in ("hardlink", "move") and not op.get("undone"):
                op_row_controls.append(get_action_icon_button(icon=ft.Icons.UNDO_ROUNDED, icon_color=WARNING_COLOR, tooltip=get_text("op_undo", current_language), on_click=lambda _, o=op: undo_operation_action(o), button_size=34, icon_size=18))
            elif op.get("undone"):
                op_row_controls.append(get_badge(get_text("op_undone", current_language), color=TEXT_MUTED))
            ops_items.append(ft.Container(
                content=ft.Row(op_row_controls, vertical_alignment=ft.CrossAxisAlignment.CENTER, spacing=10),
                bgcolor=SURFACE_HOVER,
                border_radius=8,
                padding=ft.Padding.symmetric(horizontal=10, vertical=6)
            ))
        ops_section = ft.Container()
        if ops_items:
            ops_section = get_styled_card(
                ft.Column([
                    ft.Text(get_text("ops_log_title", current_language), size=13, weight=ft.FontWeight.BOLD, color=TEXT_PRIMARY),
                    ft.Column(ops_items, spacing=6),
                ], spacing=8),
                padding=12
            )

        return ft.Column([
            get_header_row(
                title=get_text("history", current_language),
                subtitle=get_text("history_desc", current_language),
                action_control=get_outlined_button(
                    text=get_text("clear_history", current_language),
                    icon=ft.Icons.DELETE_SWEEP_ROUNDED,
                    on_click=clear_history_action,
                    color=DANGER_COLOR,
                    border_color=DANGER_COLOR,
                    height=36
                )
            ),
            ft.Row([
                get_stat_card(
                    title=get_text("total_freed_all_time", current_language),
                    value=format_file_size(total_analyzed),
                    subtitle=get_text("scan_runs_recorded", current_language).format(len(history)),
                    icon=ft.Icons.AUTO_AWESOME_ROUNDED,
                    icon_color=ACCENT_COLOR
                )
            ]),
            ops_section,
            ft.Column(history_items, scroll=ft.ScrollMode.AUTO, expand=True, spacing=8)
        ], spacing=16, expand=True)

    def build_settings_tab():
        cache_stats = cache_db.get_cache_stats()
        cache_label = ft.Text(
            get_text("cache_stats_label", current_language).format(
                cache_stats["total_files"] + cache_stats["total_images"],
                int(cache_stats["size_bytes"] / 1024)
            ),
            size=13,
            color=TEXT_MUTED
        )

        def on_clear_cache(e):
            cleared = cache_db.clear_cache()
            if cleared:
                cache_label.value = get_text("cache_stats_label", current_language).format(0, 0)
                cache_label.update()

            # 2.1b: the dialog must state the FACT — a silent clear_cache
            # failure used to show "cache cleared" anyway.
            dlg = get_styled_dialog(
                title=get_text("cache_cleared" if cleared else "cache_clear_failed", current_language),
                title_color=SUCCESS_COLOR if cleared else DANGER_COLOR,
                actions=[get_primary_button(text=get_text("ok", current_language), on_click=lambda _: page.pop_dialog(), height=36)]
            )
            page.show_dialog(dlg)

        # Context Menu State
        is_registered = is_context_menu_registered()
        context_status_text = ft.Text(
            get_text("context_menu_desc", current_language),
            size=12,
            color=TEXT_MUTED
        )

        def toggle_context_menu(e):
            if is_context_menu_registered():
                ok, err = unregister_context_menu()
                msg = get_text("context_menu_removed", current_language) if ok else f"Error: {err}"
            else:
                ok, err = register_context_menu(get_text("context_menu_action_title"))
                msg = get_text("context_menu_added", current_language) if ok else f"Error: {err}"

            dlg = get_styled_dialog(
                title=msg,
                title_color=SUCCESS_COLOR if ok else DANGER_COLOR,
                actions=[get_primary_button(text=get_text("ok", current_language), on_click=lambda _: page.pop_dialog(), height=36)]
            )
            page.show_dialog(dlg)
            # Stay on the Settings tab (index 5) and rebuild it so the
            # register/unregister button label reflects the new state — the old
            # on_nav_change(0) threw the user back to the Scanner tab.
            on_nav_change(5)

        return ft.Column([
            get_header_row(
                title=get_text("settings_title", current_language),
                subtitle=get_text("settings_desc", current_language)
            ),
            get_styled_card(
                ft.Column([
                    # Language
                    ft.Text(get_text("language_setting", current_language), size=13, weight=ft.FontWeight.W_600, color=TEXT_PRIMARY),
                    ft.Dropdown(
                        width=240,
                        value=current_language,
                        border_color=BORDER_COLOR,
                        bgcolor=SURFACE_HOVER,
                        border_radius=8,
                        text_size=13,
                        options=[
                            ft.dropdown.Option("ru", "Русский (RU)"),
                            ft.dropdown.Option("en", "English (EN)"),
                        ],
                        on_select=lambda e: set_language(e.control.value)
                    ),
                    ft.Divider(color=BORDER_COLOR, height=20),

                    # Visual Theme Switcher
                    ft.Text(get_text("theme_setting", current_language), size=13, weight=ft.FontWeight.W_600, color=TEXT_PRIMARY),
                    (lambda: (
                        ft.Row([
                            ft.Container(
                                content=ft.Row([
                                    ft.Container(width=16, height=16, border_radius=8, bgcolor=preview_bg, border=ft.Border.all(1, preview_border)),
                                    ft.Text(get_text(t_key, current_language), size=13, weight=ft.FontWeight.W_600 if get_active_theme_key() == t_id else ft.FontWeight.NORMAL, color=TEXT_PRIMARY)
                                ], spacing=10, vertical_alignment=ft.CrossAxisAlignment.CENTER),
                                padding=ft.Padding.symmetric(horizontal=14, vertical=10),
                                border_radius=10,
                                bgcolor=SURFACE_HOVER if get_active_theme_key() == t_id else ft.Colors.TRANSPARENT,
                                border=ft.Border.all(1.5, PRIMARY_COLOR if get_active_theme_key() == t_id else BORDER_COLOR),
                                on_click=lambda _, tid=t_id: change_theme(tid),
                                ink=True
                            )
                            for t_id, t_key, preview_bg, preview_border in [
                                ("dark_slate", "theme_slate", "#0B0E14", "#2A324B"),
                                ("midnight_oled", "theme_oled", "#000000", "#1A1A1A"),
                                ("light_clean", "theme_light", "#F8FAFC", "#CBD5E1"),
                            ]
                        ], spacing=12, wrap=True)
                    ))(),
                    ft.Divider(color=BORDER_COLOR, height=20),

                    # Recycle bin
                    ft.Checkbox(
                        label=get_text("default_trash_setting", current_language),
                        value=trash_default[0],
                        on_change=lambda e: set_trash_default(e.control.value),
                        disabled=not HAS_SEND2TRASH
                    ),
                    ft.Divider(color=BORDER_COLOR, height=20),

                    # Windows Explorer Context Menu
                    ft.Text(get_text("context_menu_section", current_language), size=13, weight=ft.FontWeight.W_600, color=TEXT_PRIMARY),
                    context_status_text,
                    ft.Row([
                        get_outlined_button(
                            text=get_text("remove_context_menu_btn" if is_registered else "add_context_menu_btn", current_language),
                            on_click=toggle_context_menu,
                            icon=ft.Icons.APPS_OUTLINED if not is_registered else ft.Icons.DELETE_OUTLINE_ROUNDED,
                            border_color=DANGER_COLOR if is_registered else PRIMARY_COLOR,
                            color=DANGER_COLOR if is_registered else PRIMARY_COLOR,
                            height=36
                        )
                    ]),
                    ft.Divider(color=BORDER_COLOR, height=20),

                    # SQLite Cache section
                    ft.Text(get_text("cache_section", current_language), size=13, weight=ft.FontWeight.W_600, color=TEXT_PRIMARY),
                    cache_label,
                    ft.Row([
                        get_outlined_button(
                            text=get_text("clear_cache_btn", current_language),
                            on_click=on_clear_cache,
                            icon=ft.Icons.CLEANING_SERVICES_ROUNDED,
                            border_color=WARNING_COLOR,
                            color=WARNING_COLOR,
                            height=36
                        )
                    ])
                ], spacing=10),
                padding=20
            )
        ], spacing=16, expand=True)

    # Shell Layout Navigation
    main_content_container = ft.Container(padding=20, expand=True)

    def on_nav_change(index: int):
        nonlocal current_nav_index, search_view_instance, initial_cli_dirs
        current_nav_index = index
        showing_results[0] = False

        if index in view_cache:
            # Cached views keep their state across tab switches (sweeper
            # results, compare folders, search filters).
            main_content_container.content = view_cache[index]
            if index == 0:
                search_view_instance = view_cache[0]
        elif index == 0:
            # CLI-provided folders are applied exactly once on the first visit.
            search_view_instance = SearchView(
                on_scan_start=run_scan,
                language=current_language,
                on_language_change=set_language
            )
            if initial_cli_dirs:
                search_view_instance.selected_directories = list(initial_cli_dirs)
                initial_cli_dirs = []
            view_cache[0] = search_view_instance
            if search_view_instance.selected_directories:
                search_view_instance.update_folders_list()
            main_content_container.content = search_view_instance
        elif index == 1:
            view_cache[1] = SweeperView(
                language=current_language,
                trash_default=trash_default[0]
            )
            main_content_container.content = view_cache[1]
        elif index == 2:
            view_cache[2] = SampleSearchView(
                on_scan_start=run_sample_scan,
                language=current_language
            )
            main_content_container.content = view_cache[2]
        elif index == 3:
            view_cache[3] = CompareView(
                on_compare_start=run_compare,
                language=current_language
            )
            main_content_container.content = view_cache[3]
        elif index == 4:
            # History and settings render live data — rebuilt on every visit.
            main_content_container.content = build_history_tab()
        elif index == 5:
            main_content_container.content = build_settings_tab()

        render_app_shell()
        page.update()

    def build_sidebar() -> ft.Container:
        theme = get_current_theme()
        nav_destinations = [
            (get_text("nav_scanner", current_language), ft.Icons.SEARCH_ROUNDED),
            (get_text("nav_sweeper", current_language), ft.Icons.CLEANING_SERVICES_ROUNDED),
            (get_text("nav_sample", current_language), ft.Icons.FINGERPRINT_ROUNDED),
            (get_text("nav_compare", current_language), ft.Icons.COMPARE_ARROWS_ROUNDED),
            (get_text("nav_history", current_language), ft.Icons.HISTORY_ROUNDED),
            (get_text("nav_settings", current_language), ft.Icons.SETTINGS_OUTLINED),
        ]

        nav_buttons = []
        for idx, (label, icon_name) in enumerate(nav_destinations):
            is_selected = (idx == current_nav_index)
            btn = ft.Container(
                content=ft.Row([
                    ft.Container(
                        width=3,
                        height=20,
                        border_radius=2,
                        bgcolor=theme["PRIMARY_COLOR"] if is_selected else "transparent",
                    ),
                    ft.Icon(icon_name, size=19, color=theme["PRIMARY_COLOR"] if is_selected else theme["TEXT_SECONDARY"]),
                    ft.Text(
                        label,
                        size=13,
                        weight=ft.FontWeight.W_600 if is_selected else ft.FontWeight.W_500,
                        color=theme["TEXT_PRIMARY"] if is_selected else theme["TEXT_SECONDARY"],
                        expand=True
                    )
                ], spacing=10, vertical_alignment=ft.CrossAxisAlignment.CENTER),
                bgcolor=tint(theme['PRIMARY_COLOR'], "1C") if is_selected else "transparent",
                border=ft.Border.all(1, tint(theme['PRIMARY_COLOR'], "44")) if is_selected else ft.Border.all(1, "transparent"),
                border_radius=10,
                padding=ft.Padding.symmetric(horizontal=8, vertical=9),
                on_click=lambda _, i=idx: on_nav_change(i),
                ink=True
            )
            nav_buttons.append(btn)

        # Brand Header & Language Switcher
        return ft.Container(
            content=ft.Column([
                ft.Container(
                    content=ft.Row([
                        ft.Container(
                            content=ft.Icon(ft.Icons.COPY_ALL_ROUNDED, color=ft.Colors.WHITE, size=22),
                            bgcolor=theme["PRIMARY_COLOR"],
                            border_radius=10,
                            padding=8,
                            shadow=ft.BoxShadow(
                                blur_radius=8,
                                spread_radius=0,
                                color=tint(theme['PRIMARY_COLOR'], "55"),
                                offset=ft.Offset(0, 2)
                            )
                        ),
                        ft.Column([
                            ft.Text(APP_NAME, size=15, weight=ft.FontWeight.BOLD, color=theme["TEXT_PRIMARY"]),
                            ft.Text(f"{APP_TAGLINE} v{APP_VERSION}", size=11, color=theme["TEXT_MUTED"]),
                        ], spacing=1)
                    ], spacing=12, vertical_alignment=ft.CrossAxisAlignment.CENTER),
                    padding=ft.Padding.only(bottom=10, top=4, left=4, right=4)
                ),
                ft.Divider(color=theme["BORDER_COLOR"], height=1),

                ft.Column(nav_buttons, spacing=4, expand=True),

                ft.Divider(color=theme["BORDER_COLOR"], height=1),
                ft.Container(
                    content=ft.Row([
                        ft.Container(
                            content=ft.Row([
                                ft.Container(
                                    content=ft.Text("RU", size=12, weight=ft.FontWeight.BOLD if current_language == "ru" else ft.FontWeight.NORMAL, color="#FFFFFF" if current_language == "ru" else theme["TEXT_MUTED"]),
                                    bgcolor=theme["PRIMARY_COLOR"] if current_language == "ru" else "transparent",
                                    border_radius=6,
                                    padding=ft.Padding.symmetric(horizontal=10, vertical=5),
                                    on_click=lambda _: set_language("ru"),
                                    ink=True
                                ),
                                ft.Container(
                                    content=ft.Text("EN", size=12, weight=ft.FontWeight.BOLD if current_language == "en" else ft.FontWeight.NORMAL, color="#FFFFFF" if current_language == "en" else theme["TEXT_MUTED"]),
                                    bgcolor=theme["PRIMARY_COLOR"] if current_language == "en" else "transparent",
                                    border_radius=6,
                                    padding=ft.Padding.symmetric(horizontal=10, vertical=5),
                                    on_click=lambda _: set_language("en"),
                                    ink=True
                                ),
                            ], spacing=2),
                            bgcolor=theme["SURFACE_CARD"],
                            border=ft.Border.all(1, theme["BORDER_COLOR"]),
                            border_radius=8,
                            padding=2
                        ),
                        ft.Container(expand=True),
                        ft.Container(
                            content=ft.Text(f"v{APP_VERSION}", size=11, color=theme["TEXT_MUTED"], weight=ft.FontWeight.W_500),
                            padding=ft.Padding.only(right=4)
                        )
                    ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN, vertical_alignment=ft.CrossAxisAlignment.CENTER),
                    padding=ft.Padding.only(top=4, bottom=4)
                )
            ], spacing=8),
            width=236,
            bgcolor=theme["SURFACE_COLOR"],
            border=ft.Border.only(right=ft.BorderSide(1, theme["BORDER_COLOR"])),
            padding=ft.Padding.symmetric(horizontal=14, vertical=16)
        )

    def render_app_shell():
        sidebar = build_sidebar()
        page.clean()
        page.add(
            ft.Row([
                sidebar,
                main_content_container
            ], expand=True, spacing=0)
        )

    on_nav_change(0)

if __name__ == "__main__":
    ft.run(main)
