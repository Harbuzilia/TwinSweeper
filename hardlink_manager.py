import os
import ctypes
from typing import Tuple, List, Dict

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

    Returns (success, error_message, freed_bytes).
    """
    if not os.path.exists(source_original) or not os.path.isfile(source_original):
        return False, f"Original file not found: {source_original}", 0
    if not os.path.exists(target_duplicate) or not os.path.isfile(target_duplicate):
        return False, f"Duplicate file not found: {target_duplicate}", 0

    if os.path.normpath(source_original).lower() == os.path.normpath(target_duplicate).lower():
        return False, "Source and target are the same file.", 0

    if not is_same_volume(source_original, target_duplicate):
        return False, f"Cannot hardlink across different disk volumes ({source_original} vs {target_duplicate}).", 0

    # Verify if they are already the same hardlink
    try:
        if os.path.samefile(source_original, target_duplicate):
            return True, "", 0  # Already linked: nothing left to free.
    except Exception:
        pass

    try:
        dup_size = os.path.getsize(target_duplicate)
    except OSError:
        dup_size = 0

    temp_link = target_duplicate + f".tmp_hl_{os.getpid()}"
    backup = target_duplicate + f".tmp_hl_backup_{os.getpid()}"
    try:
        # Create hardlink at temporary path first
        if os.name == 'nt':
            # Use CreateHardLinkW API for Windows NTFS
            res = ctypes.windll.kernel32.CreateHardLinkW(temp_link, source_original, None)
            if not res:
                err = ctypes.GetLastError()
                return False, f"Windows CreateHardLink error code {err}", 0
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
                except Exception:
                    pass
            return False, str(ex), 0

        # Path now points to the shared inode; drop the duplicate's old data.
        try:
            os.remove(backup)
        except OSError:
            pass
        return True, "", dup_size
    except Exception as ex:
        if os.path.exists(temp_link):
            try:
                os.remove(temp_link)
            except Exception:
                pass
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
            errors.append(f"Original file missing: {original}")
            continue

        for dup in duplicates:
            ok, err, freed = replace_with_hardlink(original, dup)
            if ok:
                success_count += 1
                freed_bytes += freed
                succeeded_paths.append(dup)
            else:
                errors.append(f"Failed to hardlink '{os.path.basename(dup)}': {err}")

    return success_count, freed_bytes, errors, succeeded_paths
