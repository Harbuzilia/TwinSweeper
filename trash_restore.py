"""Recover files from the Windows Recycle Bin.

``send2trash`` moves a file to the bin but exposes no way back, so the undo
path of a trash deletion has to read the bin itself. Windows keeps two files
per deleted item in a per-volume, per-user folder::

    <volume>:\\$Recycle.Bin\\<SID>\\$I<id><ext>   metadata index
    <volume>:\\$Recycle.Bin\\<SID>\\$R<id><ext>   the original data

The ``$I`` record is the only place the original path survives:

* version 1 (Windows 95/98/ME, 280 bytes): header, size, FILETIME, then the
  original path as a NUL-terminated ANSI string (260 bytes max);
* version 2 (Windows NT/2000+, 800 bytes): header, size, FILETIME, the path
  length in UTF-16 code units, then the path as UTF-16LE.

Data safety — the mirror image of the H5 rule on the delete path: a restore
that fails NEVER removes the bin entry. The data file is copied to a temp
name next to its original location, swapped into place atomically, and only
then is the bin copy unlinked; a failure at any earlier point leaves the bin
untouched and the user's file still recoverable.

Only files are restorable. TwinSweeper deletes files through ``send2trash``
and removes emptied folders with ``os.rmdir`` (they never enter the bin), so
a directory entry is reported as unsupported instead of being half-restored.
"""
import os
import shutil
import string
import struct
import uuid
from dataclasses import dataclass
from typing import Dict, Iterable, Iterator, List, Sequence, Set, Tuple

from app_logging import get_logger
from locales import get_text

logger = get_logger(__name__)

# 100-nanosecond ticks between 1601-01-01 (FILETIME epoch) and 1970-01-01.
FILETIME_UNIX_EPOCH = 116444736000000000
FILETIME_TICKS_PER_SECOND = 10_000_000

# $I header versions we understand; anything else is not a recycle index.
_INDEX_VERSION_V1 = 1
_INDEX_VERSION_V2 = 2

# Version 1 records are fixed-size; the ANSI path field is 256 bytes.
_V1_HEADER_SIZE = 24
_V1_RECORD_SIZE = 280


class TrashError(Exception):
    """Base class for restore failures.

    The message is translated lazily (``str(exc)``), so a failure raised from a
    worker thread is rendered in whatever language the UI currently uses.
    """

    key = "restore_failed"

    def __init__(self, *params: object) -> None:
        self.params = params
        super().__init__(*params)

    def __str__(self) -> str:
        template = get_text(self.key)
        try:
            return template.format(*self.params)
        except (IndexError, KeyError, ValueError):
            # A broken translation must never mask the real failure.
            return " ".join(str(p) for p in self.params) or self.key


class TrashEntryNotFound(TrashError):
    """No $I record in any bin lists the requested original path."""

    key = "restore_not_in_trash"


class TrashPathConflict(TrashError):
    """Something already occupies the original path."""

    key = "restore_path_conflict"


class TrashEntryUnreadable(TrashError):
    """The bin entry exists but its data file is gone or unreadable."""

    key = "restore_entry_unreadable"


class TrashDirectoryUnsupported(TrashError):
    """Folders are not restorable from the bin by this module."""

    key = "restore_dir_unsupported"


class TrashRestoreFailed(TrashError):
    """Copying the bin data back to the original path failed."""

    key = "restore_failed"


@dataclass(frozen=True)
class ParsedIndex:
    """Decoded ``$I`` record."""

    original_path: str
    size: int
    deleted_ts: float


@dataclass(frozen=True)
class TrashEntry:
    """One ``$I``/``$R`` pair found in a bin."""

    index_path: str
    data_path: str
    original_path: str
    size: int
    deleted_ts: float
    is_directory: bool = False


def filetime_to_timestamp(value: int) -> float:
    """Converts a Windows FILETIME (100 ns since 1601) to a Unix timestamp."""
    return (value - FILETIME_UNIX_EPOCH) / FILETIME_TICKS_PER_SECOND


def _decode_v1_path(raw: bytes) -> str:
    # Version 1 stored the path in the ANSI code page of the deleting system.
    encoding = "mbcs" if os.name == "nt" else "latin-1"
    try:
        text = raw.decode(encoding, errors="replace")
    except LookupError:  # pragma: no cover - non-Windows fallback
        text = raw.decode("latin-1", errors="replace")
    return text.split("\x00", 1)[0]


def parse_index_bytes(data: bytes) -> ParsedIndex | None:
    """Decodes an ``$I`` record; returns None when it is not one."""
    if len(data) < _V1_HEADER_SIZE:
        return None
    version, size, deleted = struct.unpack("<qqq", data[:24])
    if version == _INDEX_VERSION_V2:
        if len(data) < 28:
            return None
        (chars,) = struct.unpack("<i", data[24:28])
        if chars < 0:
            return None
        raw = data[28:28 + chars * 2]
        original = raw.decode("utf-16-le", errors="replace").rstrip("\x00")
    elif version == _INDEX_VERSION_V1:
        original = _decode_v1_path(data[_V1_HEADER_SIZE:_V1_RECORD_SIZE])
    else:
        return None
    original = original.strip()
    if not original:
        return None
    return ParsedIndex(original_path=original, size=size, deleted_ts=filetime_to_timestamp(deleted))


def read_index_file(index_path: str) -> ParsedIndex | None:
    """Reads and decodes one ``$I`` file; unreadable files yield None."""
    try:
        with open(index_path, "rb") as f:
            return parse_index_bytes(f.read())
    except OSError as ex:
        logger.debug("recycle index unreadable (%s): %s", index_path, ex)
        return None


def _pair_entries(directory: str) -> Iterator[TrashEntry]:
    """Yields the ``$I``/``$R`` pairs that live directly in *directory*."""
    try:
        names = os.listdir(directory)
    except OSError as ex:
        logger.debug("recycle folder unreadable (%s): %s", directory, ex)
        return
    by_lower = {name.lower(): name for name in names}
    for name in names:
        if not name.upper().startswith("$I") or len(name) <= 2:
            continue
        suffix = name[2:]
        data_name = by_lower.get(("$r" + suffix).lower())
        if data_name is None:
            # An index without data cannot restore anything (Explorer shows a
            # broken entry; we simply skip it).
            continue
        index_path = os.path.join(directory, name)
        data_path = os.path.join(directory, data_name)
        parsed = read_index_file(index_path)
        if parsed is None:
            logger.debug("skipping undecodable recycle index: %s", index_path)
            continue
        yield TrashEntry(
            index_path=index_path,
            data_path=data_path,
            original_path=parsed.original_path,
            size=parsed.size,
            deleted_ts=parsed.deleted_ts,
            is_directory=os.path.isdir(data_path),
        )


def iter_trash_entries(recycle_bin_dir: str) -> Iterator[TrashEntry]:
    """Enumerates a bin folder: pairs at the root plus one level of SID dirs."""
    if not os.path.isdir(recycle_bin_dir):
        return
    yield from _pair_entries(recycle_bin_dir)
    try:
        children = os.listdir(recycle_bin_dir)
    except OSError:
        return
    for child in children:
        sub = os.path.join(recycle_bin_dir, child)
        # Per-user folders (S-1-5-21-...) hold the pairs on NTFS volumes.
        if child.startswith("$") or not os.path.isdir(sub):
            continue
        yield from _pair_entries(sub)


def recycle_bin_dirs(roots: Sequence[str] | None = None) -> List[str]:
    """Existing Recycle Bin roots.

    *roots* is for tests and for callers that already know the volumes; when
    omitted every mounted drive on Windows is probed for ``$Recycle.Bin`` (and
    the pre-Vista ``RECYCLER`` folder).
    """
    if roots is not None:
        return [root for root in roots if os.path.isdir(root)]
    if os.name != "nt":
        return []
    found: List[str] = []
    for letter in string.ascii_uppercase:
        drive = f"{letter}:\\"
        if not os.path.isdir(drive):
            continue
        for folder in ("$Recycle.Bin", "RECYCLER"):
            candidate = os.path.join(drive, folder)
            if os.path.isdir(candidate):
                found.append(candidate)
    return found


def recycle_bin_available(recycle_dirs: Sequence[str] | None = None) -> bool:
    """Whether any Recycle Bin folder can be scanned on this system.

    ``False`` means the Restore action has no source at all — not Windows, or
    every mounted volume without a bin — which the History tab states out loud
    instead of silently offering nothing (``restore_unavailable``).
    """
    return bool(recycle_bin_dirs(recycle_dirs))


def trash_key(path: str) -> str:
    """Normalised lookup key for an original path."""
    return os.path.normcase(os.path.normpath(os.path.abspath(path)))


def build_index(recycle_dirs: Sequence[str] | None = None) -> Dict[str, List[TrashEntry]]:
    """Maps normalised original path -> entries, newest deletion first."""
    index: Dict[str, List[TrashEntry]] = {}
    for directory in recycle_bin_dirs(recycle_dirs):
        for entry in iter_trash_entries(directory):
            index.setdefault(trash_key(entry.original_path), []).append(entry)
    for entries in index.values():
        entries.sort(key=lambda e: e.deleted_ts, reverse=True)
    return index


def _remove_bin_entry(entry: TrashEntry) -> None:
    """Drops the bin copy — called only after the file is back in place."""
    for path in (entry.data_path, entry.index_path):
        try:
            if os.path.isdir(path) and not os.path.islink(path):
                os.rmdir(path)
            elif os.path.exists(path):
                os.remove(path)
        except OSError as ex:
            # The restore already succeeded; a lingering bin copy is harmless
            # (Explorer hides $R files without their $I index).
            logger.warning("could not remove bin entry %s: %s", path, ex)


def restore_entry(entry: TrashEntry, overwrite: bool = False) -> None:
    """Puts one bin entry back at its original path.

    Raises a :class:`TrashError` subclass on failure; in every failure case the
    bin entry is left intact.
    """
    target = entry.original_path
    if entry.is_directory or os.path.isdir(entry.data_path):
        raise TrashDirectoryUnsupported(target)
    if not os.path.isfile(entry.data_path):
        raise TrashEntryUnreadable(entry.data_path)
    if os.path.exists(target) and not overwrite:
        raise TrashPathConflict(target)

    parent = os.path.dirname(target)
    # Unique per attempt (pid alone collides between concurrent restores of the
    # same path), and kept next to the target so ``os.replace`` stays atomic.
    temp_path = f"{target}.twinsweeper_restore_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    try:
        if parent:
            os.makedirs(parent, exist_ok=True)
        if os.path.exists(temp_path):
            os.remove(temp_path)
        # Full copy first: a half-written restore must never land at the target.
        shutil.copy2(entry.data_path, temp_path)
        os.replace(temp_path, target)
    except Exception as ex:
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except OSError:
            pass
        raise TrashRestoreFailed(target, ex) from ex

    _remove_bin_entry(entry)


def restore_file(
    original_path: str,
    recycle_dirs: Sequence[str] | None = None,
    overwrite: bool = False,
    index: Dict[str, List[TrashEntry]] | None = None,
) -> TrashEntry:
    """Restores the newest bin copy of *original_path*; returns that entry."""
    if index is None:
        index = build_index(recycle_dirs)
    entries = index.get(trash_key(original_path))
    if not entries:
        raise TrashEntryNotFound(original_path)
    entry = entries[0]
    restore_entry(entry, overwrite=overwrite)
    return entry


def restore_paths(
    paths: Iterable[str],
    recycle_dirs: Sequence[str] | None = None,
    overwrite: bool = False,
) -> Tuple[int, List[str]]:
    """Restores many files in one pass (the bin is enumerated once).

    Returns ``(restored_count, errors)`` — the same shape as the other undo
    helpers, so the journal and the UI can treat them alike.

    Retrying a partial restore is idempotent: a path that is no longer in the
    bin *and* whose file sits at the original location counts as restored. The
    bin entry was consumed by an earlier attempt (or the user put the file back
    by hand), so there is nothing left to do and — crucially — nothing to
    report: an error here would keep a finished operation "retryable" forever.
    A path that is missing from the bin with no file at the target is still an
    error, since the data really is gone.
    """
    index = build_index(recycle_dirs)
    restored = 0
    errors: List[str] = []
    for path in paths:
        key = trash_key(path)
        if key not in index and os.path.isfile(path):
            # Already back in place: success without touching anything.
            restored += 1
            continue
        try:
            restore_file(path, recycle_dirs=recycle_dirs, overwrite=overwrite, index=index)
            restored += 1
        except TrashError as ex:
            errors.append(str(ex))
        except Exception as ex:  # never let one bad path abort the batch
            errors.append(get_text("restore_failed").format(path, ex))
    return restored, errors


def restorable_paths(paths: Iterable[str], recycle_dirs: Sequence[str] | None = None) -> Set[str]:
    """Subset of *paths* that still has a file sitting in the bin.

    Used by the History tab to decide whether a Restore button makes sense;
    folders are excluded because they cannot be restored. The scan stops as
    soon as every requested path is accounted for — the History tab runs this
    on the UI thread, so a big bin must not cost more than it has to.
    """
    wanted = {trash_key(path): path for path in paths}
    found: Set[str] = set()
    if not wanted:
        return found
    for directory in recycle_bin_dirs(recycle_dirs):
        for entry in iter_trash_entries(directory):
            if entry.is_directory:
                continue
            original = wanted.get(trash_key(entry.original_path))
            if original is None:
                continue
            found.add(original)
            if len(found) == len(wanted):
                return found
    return found
