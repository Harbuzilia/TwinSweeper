"""Shared filesystem traversal filters — one canonical implementation for
all three scanners (duplicates, similar images, sweeper).

Rules every walk must agree on (M2/M3):

* always-excluded system locations ($Recycle.Bin, System Volume Information)
  are pruned by whole directory-NAME component, never as substrings: a
  deleted file's copy in the Recycle Bin keeps the original's mtime, so it
  would otherwise be grouped with the live file and — being older — win the
  "original/keep" slot, marking the real file as the deletable "duplicate";
* Windows junctions are pruned explicitly: os.walk(followlinks=False) only
  refuses symlinks (os.path.islink) — junctions pass as plain folders and
  cause double walks and even infinite loops;
* one physical file must never be indexed twice: hardlinks, junction/symlink
  aliases and overlapping scan roots all resolve to the same (device, inode);
  grouping such a pair as "duplicates" would let the user delete a file and
  thereby destroy the very "original" it pointed at.
"""
import os
from typing import List, Optional, Set, Tuple

# Known system locations matched as whole path components, never as substrings
# (M2: substring matching flagged paths like E:\bootcamp\notes.txt).
_ROOT_LEVEL_SYSTEM_DIRS = {
    "$recycle.bin", "system volume information", "$windows.~bt", "$windows.~ws",
    "recovery", "boot", "config.msi", "msocache", "windows",
    "program files", "program files (x86)", "programdata",
}
_SYSTEM_DIR_COMPONENTS = {"system32", "syswow64", "winsxs"}

# Locations pruned from EVERY scan regardless of user exclude patterns: a
# deleted file's copy in the Recycle Bin keeps the original's mtime, so it
# would otherwise be grouped with the live file and — being older — win the
# "original/keep" slot, marking the real file as the deletable "duplicate".
_ALWAYS_EXCLUDED_DIRS = {"$recycle.bin", "system volume information"}


def _system_roots() -> List[str]:
    """Real system root paths from the environment plus common fallbacks.

    Computed per call so tests (and unusual setups) can override via env vars.
    """
    roots = []
    for var in ("SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "ProgramData"):
        value = os.environ.get(var)
        if value:
            roots.append(os.path.normcase(os.path.abspath(value)))
    roots.extend([
        r"c:\windows",
        r"c:\program files",
        r"c:\program files (x86)",
        r"c:\programdata",
    ])
    return roots


def is_system_path(path: str) -> bool:
    """Checks if the path belongs to a protected Windows system directory or file.

    Component-based matching (M2): a path is system when its first directory
    component is a known system folder (on any drive), when any component is a
    system-only directory (system32/winsxs/...), or when it lives under a real
    system root taken from the environment. User files merely named like system
    paths (E:\\bootcamp\\notes.txt, E:\\Games\\mod.dll) are NOT flagged.
    """
    try:
        norm = os.path.normcase(os.path.abspath(path))
    except (OSError, ValueError):
        return False

    parts = norm.split(os.sep)
    if parts and parts[0].endswith(":"):
        parts = parts[1:]
    parts = [p for p in parts if p]
    if not parts:
        return False

    if parts[0] in _ROOT_LEVEL_SYSTEM_DIRS:
        return True
    if any(p in _SYSTEM_DIR_COMPONENTS for p in parts):
        return True

    for root in _system_roots():
        if norm == root or norm.startswith(root + os.sep):
            return True

    return False


def prune_dirs(root: str, dirs: List[str]) -> List[str]:
    """In-place filter of os.walk's ``dirs`` list for topdown walks.

    Removes always-excluded system locations (matched as whole directory NAME
    components) and Windows junctions, so the walker never descends into them
    and a junction node is never even visited as a folder. Mutating the SAME
    list object is what makes os.walk(topdown=True) skip the pruned subtrees.

    Returns the same (mutated) list so callers can chain their own
    pattern-based pruning on top.
    """
    dirs[:] = [
        d for d in dirs
        if d.lower() not in _ALWAYS_EXCLUDED_DIRS
        and not os.path.isjunction(os.path.join(root, d))  # Python 3.12+ (os.path.isjunction)
    ]
    return dirs


def inode_key(stat: os.stat_result) -> Optional[Tuple[int, int]]:
    """Physical file identity ``(st_dev, st_ino)``, or None when the
    filesystem reports no inode (FAT/exFAT report st_ino == 0 for every
    file) — such files can never be alias-deduplicated."""
    if not stat.st_ino:
        return None
    return (stat.st_dev, stat.st_ino)


class InodeDeduper:
    """Deduplicates one physical file seen under several paths.

    Hardlinks, junction/symlink aliases and overlapping scan roots resolve to
    the same (device, inode); reporting such a pair as "duplicates" would let
    the user delete a file and thereby destroy the very "original" it pointed
    at. Files without a usable inode (st_ino == 0) are never deduplicated.
    """

    def __init__(self) -> None:
        self._seen: Set[Tuple[int, int]] = set()

    def already_seen(self, stat: os.stat_result) -> bool:
        """Registers the stat and returns True if this physical file was
        already registered before."""
        key = inode_key(stat)
        if key is None:
            return False
        if key in self._seen:
            return True
        self._seen.add(key)
        return False
