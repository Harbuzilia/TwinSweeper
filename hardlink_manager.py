import os
import ctypes
import ctypes.wintypes
import glob
import logging
from typing import Tuple, List, Dict

from scanner import get_file_hash
from locales import get_text

logger = logging.getLogger(__name__)

# CreateHardLinkW with proper prototypes and use_last_error=True so the
# Win32 error code is fetched reliably instead of whatever GetLastError()
# happened to hold (M1).
try:
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.CreateHardLinkW.argtypes = [
        ctypes.c_wchar_p,  # lpFileName
        ctypes.c_wchar_p,  # lpExistingFileName
        ctypes.c_void_p,   # lpSecurityAttributes
    ]
    _kernel32.CreateHardLinkW.restype = ctypes.wintypes.BOOL
except Exception:  # pragma: no cover - only hit on non-Windows
    _kernel32 = None


def is_same_volume(path1: str, path2: str) -> bool:
    """Checks whether two paths reside on the exact same disk volume/partition."""
    try:
        drive1 = os.path.splitdrive(os.path.abspath(path1))[0].lower()
        drive2 = os.path.splitdrive(os.path.abspath(path2))[0].lower()
        return bool(drive1 and drive1 == drive2)
    except Exception:
        return False

def replace_with_hardlink(source_original: str, target_duplicate: str) -> Tuple[bool, str, int]:
    """
    Safely replaces duplicate file with an NTFS hardlink to the original file.
    Reclaims disk space without breaking file paths or deleting files from applications.

    The duplicate is first renamed aside (never a window where its path is missing or
    its data is lost), the hardlink is moved into place, and only then the old data is
    dropped. On failure the original duplicate is restored.

    Refuses to link files whose content is not byte-identical: a hardlink between
    different content would silently destroy the duplicate's data (C1).

    Returns (success, error_message, freed_bytes).
    """
    if not os.path.exists(source_original) or not os.path.isfile(source_original):
        return False, get_text("hl_err_original_not_found").format(source_original), 0

    # Recover from a crash in a PREVIOUS run that happened between the two
    # renames: the duplicate is then orphaned at a .tmp_hl_backup_<oldpid>
    # path with nothing at the target. Restore it so the file is not lost
    # (round 3 — the old code just reported "duplicate not found").
    if not os.path.exists(target_duplicate):
        orphans = sorted(glob.glob(target_duplicate + ".tmp_hl_backup_*"))
        if orphans:
            try:
                os.replace(orphans[-1], target_duplicate)
            except OSError as ex:
                logger.warning("could not restore orphaned hardlink backup %s: %s", orphans[-1], ex)

    if not os.path.exists(target_duplicate) or not os.path.isfile(target_duplicate):
        return False, get_text("hl_err_duplicate_not_found").format(target_duplicate), 0

    if os.path.normpath(source_original).lower() == os.path.normpath(target_duplicate).lower():
        return False, get_text("hl_err_same_file"), 0

    if not is_same_volume(source_original, target_duplicate):
        return False, f"Cannot hardlink across different disk volumes ({source_original} vs {target_duplicate}).", 0

    # Verify if they are already the same hardlink
    try:
        if os.path.samefile(source_original, target_duplicate):
            return True, "", 0  # Already linked: nothing left to free.
    except Exception as ex:
        logger.debug("samefile check failed for %s vs %s: %s", source_original, target_duplicate, ex)

    try:
        src_size = os.path.getsize(source_original)
        dup_size = os.path.getsize(target_duplicate)
    except OSError as ex:
        return False, str(ex), 0

    # C1 guard: only byte-identical files may be hardlinked. Sizes must match
    # and full SHA-256 must match — this protects visually-similar (but
    # different) photos selected for hardlinking by mistake.
    if src_size != dup_size:
        return False, get_text("hl_err_size_diff"), 0
    src_hash = get_file_hash(source_original)
    dup_hash = get_file_hash(target_duplicate)
    if not src_hash or not dup_hash or src_hash != dup_hash:
        return False, get_text("hl_err_content_diff"), 0

    temp_link = target_duplicate + f".tmp_hl_{os.getpid()}"
    backup = target_duplicate + f".tmp_hl_backup_{os.getpid()}"

    # M6: stale temp files from a previously crashed run would make
    # CreateHardLinkW fail (file already exists) and are never cleaned up
    # anywhere else — remove them before retrying.
    for stale in (temp_link, backup):
        if os.path.exists(stale):
            try:
                os.remove(stale)
            except OSError as ex:
                return False, get_text("hl_err_stale_tmp").format(stale, ex), 0

    try:
        # Create hardlink at temporary path first
        if os.name == 'nt':
            if _kernel32 is not None:
                res = _kernel32.CreateHardLinkW(temp_link, source_original, None)
                if not res:
                    err = ctypes.get_last_error()
                    return False, get_text("hl_err_win32").format(err), 0
            else:  # pragma: no cover - fallback when ctypes is unavailable
                os.link(source_original, temp_link)
        else:
            os.link(source_original, temp_link)

        # Move the duplicate aside, then move the hardlink into its place.
        try:
            os.rename(target_duplicate, backup)
            try:
                os.rename(temp_link, target_duplicate)
            except Exception:
                # Rollback: restore the duplicate exactly where it was.
                os.rename(backup, target_duplicate)
                raise
        except Exception as ex:
            if os.path.exists(temp_link):
                try:
                    os.remove(temp_link)
                except Exception as cleanup_ex:
                    logger.warning("temp file left behind after failed hardlink: %s (%s)", temp_link, cleanup_ex)
            return False, str(ex), 0

        # Path now points to the shared inode; drop the duplicate's old data.
        # If the removal fails the hardlink is still in place — report success
        # but no freed space and mention the leftover (M6).
        try:
            os.remove(backup)
        except OSError as ex:
            logger.warning("Hardlink created but old copy could not be removed: %s (%s)", backup, ex)
            return True, get_text("hl_warn_backup_leftover").format(backup), 0
        return True, "", dup_size
    except Exception as ex:
        if os.path.exists(temp_link):
            try:
                os.remove(temp_link)
            except Exception as cleanup_ex:
                logger.warning("temp file left behind after failed hardlink: %s (%s)", temp_link, cleanup_ex)
        return False, str(ex), 0

def batch_replace_with_hardlinks(groups_to_link: Dict[str, List[str]]) -> Tuple[int, int, List[str], List[str]]:
    """
    Processes duplicate groups: keeps group[0] as original and hardlinks group[1:].
    Returns (success_count, freed_bytes, errors, succeeded_paths).
    """
    success_count = 0
    freed_bytes = 0
    errors = []
    succeeded_paths = []

    for original, duplicates in groups_to_link.items():
        if not os.path.exists(original):
            errors.append(get_text("hl_err_original_missing").format(original))
            continue

        for dup in duplicates:
            # Already the same physical file (a pre-existing hardlink): there
            # is nothing to free, and journaling it would let "undo" unlink
            # files the user linked themselves long ago (round 3).
            try:
                if os.path.samefile(original, dup):
                    continue
            except OSError:
                pass
            ok, err, freed = replace_with_hardlink(original, dup)
            if ok:
                success_count += 1
                freed_bytes += freed
                succeeded_paths.append(dup)
                if err:
                    errors.append(get_text("hl_msg_linked_with_warning").format(os.path.basename(dup), err))
            else:
                errors.append(get_text("hl_msg_failed").format(os.path.basename(dup), err))

    return success_count, freed_bytes, errors, succeeded_paths
