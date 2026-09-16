import os
import struct
from typing import List, Dict, Tuple, Optional, Callable
from scanner import FileInfo, is_system_path

JUNK_EXTENSIONS = {".tmp", ".bak", ".old", ".dmp", ".log", ".gid", ".chk"}
# desktop.ini is NOT junk (H4): it is a legitimate Windows folder-customization
# file — deleting it breaks folder icons/views.
JUNK_FILENAMES = {"thumbs.db", ".ds_store", "$recycle.bin"}

def find_empty_directories(
    directories: List[str],
    cancel_flag: Optional[List[bool]] = None,
    progress_callback: Optional[Callable[[str], None]] = None
) -> List[str]:
    """
    Finds empty directories and recursively empty subdirectories in bottom-up order.
    """
    empty_folders = []
    total_checked = 0

    for directory in directories:
        if not os.path.exists(directory):
            continue

        # Walk bottom-up so leaf directories are inspected first
        for root, dirs, files in os.walk(directory, topdown=False, followlinks=False):
            if cancel_flag and cancel_flag[0]:
                return empty_folders

            total_checked += 1
            if progress_callback and total_checked % 50 == 0:
                progress_callback(f"Checked {total_checked} directories...")

            # Don't delete the root search directories themselves
            if os.path.normpath(root).lower() == os.path.normpath(directory).lower():
                continue

            # Check if directory contains no files and no subdirectories (or only already empty dirs)
            try:
                contents = os.listdir(root)
                # Filter out contents that are already in empty_folders
                remaining = [c for c in contents if os.path.join(root, c) not in empty_folders]
                if not remaining:
                    empty_folders.append(root)
            except (OSError, PermissionError):
                continue

    return empty_folders

def delete_empty_directories(folders: List[str]) -> Tuple[int, List[str]]:
    """Removes empty directories (ordered deepest first)."""
    deleted_count = 0
    errors = []

    for folder in folders:
        try:
            os.rmdir(folder)
            deleted_count += 1
        except Exception as ex:
            errors.append(f"Failed to remove '{folder}': {ex}")

    return deleted_count, errors

def resolve_windows_shortcut_target(lnk_path: str) -> Optional[str]:
    """
    Parses Windows ShellLink (.lnk) binary structure to extract target path without external dependencies.
    """
    try:
        with open(lnk_path, 'rb') as f:
            content = f.read()

        # ShellLink header check: HeaderSize (4 bytes == 0x4C), LinkCLSID (16 bytes)
        if len(content) < 0x4C or content[:4] != b'\x4c\x00\x00\x00':
            return None

        # Flags are at offset 0x14
        flags = struct.unpack('<I', content[0x14:0x18])[0]
        has_link_target_id_list = bool(flags & 0x01)
        has_link_info = bool(flags & 0x02)

        offset = 0x4C

        # Skip LinkTargetIDList if present
        if has_link_target_id_list:
            id_list_size = struct.unpack('<H', content[offset:offset + 2])[0]
            offset += 2 + id_list_size

        # Extract LinkInfo
        if has_link_info and len(content) >= offset + 0x1C:
            link_info_size = struct.unpack('<I', content[offset:offset + 4])[0]
            link_info_flags = struct.unpack('<I', content[offset + 0x08:offset + 0x0C])[0]
            local_base_path_offset = struct.unpack('<I', content[offset + 0x10:offset + 0x14])[0]

            # LinkInfoFlags bit 0 = VolumeIdAndLocalBasePath; offset 0 means the
            # shortcut has no local base path (network-only) and must not be
            # parsed as one (M10) — that produced garbage targets from
            # unrelated header bytes.
            if (link_info_flags & 0x1) and 0 < local_base_path_offset < link_info_size:
                path_start = offset + local_base_path_offset
                path_end = content.find(b'\x00', path_start)
                if path_end != -1:
                    raw_path = content[path_start:path_end]
                    # Try windows-1251 / utf-8
                    try:
                        return raw_path.decode('utf-8')
                    except UnicodeDecodeError:
                        return raw_path.decode('cp1251', errors='ignore')
    except Exception:
        pass
    return None

def find_broken_shortcuts(
    directories: List[str],
    cancel_flag: Optional[List[bool]] = None,
    progress_callback: Optional[Callable[[str], None]] = None
) -> List[Dict[str, str]]:
    """Finds .lnk shortcut files whose target file/folder does not exist."""
    broken = []
    scanned = 0

    for directory in directories:
        if not os.path.exists(directory):
            continue
        for root, _, filenames in os.walk(directory, followlinks=False):
            if cancel_flag and cancel_flag[0]:
                return broken

            for filename in filenames:
                if filename.lower().endswith(".lnk"):
                    scanned += 1
                    if progress_callback and scanned % 10 == 0:
                        progress_callback(f"Analyzed {scanned} shortcuts...")

                    filepath = os.path.join(root, filename)
                    target = resolve_windows_shortcut_target(filepath)
                    if target and not os.path.exists(target):
                        try:
                            stat = os.stat(filepath)
                            broken.append({
                                "path": filepath,
                                "name": filename,
                                "target": target,
                                "size": stat.st_size
                            })
                        except OSError:
                            continue

    return broken

def find_junk_files(
    directories: List[str],
    cancel_flag: Optional[List[bool]] = None,
    progress_callback: Optional[Callable[[str], None]] = None
) -> List[FileInfo]:
    """Finds temporary, backup, and junk files."""
    junk = []
    scanned = 0

    for directory in directories:
        if not os.path.exists(directory):
            continue
        for root, _, filenames in os.walk(directory, followlinks=False):
            if cancel_flag and cancel_flag[0]:
                return junk

            for filename in filenames:
                scanned += 1
                if progress_callback and scanned % 100 == 0:
                    progress_callback(f"Scanned {scanned} files for junk...")

                filepath = os.path.join(root, filename)
                name_lower = filename.lower()
                ext = os.path.splitext(name_lower)[1]

                if ext in JUNK_EXTENSIONS or name_lower in JUNK_FILENAMES:
                    if not is_system_path(filepath):
                        try:
                            stat = os.stat(filepath)
                            junk.append(FileInfo(
                                path=filepath,
                                name=filename,
                                size=stat.st_size,
                                created=stat.st_ctime,
                                modified=stat.st_mtime,
                                category="other"
                            ))
                        except (OSError, PermissionError):
                            continue

    return junk
