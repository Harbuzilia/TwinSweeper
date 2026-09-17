import os
import hashlib
import fnmatch
from collections import defaultdict
from dataclasses import dataclass
from typing import List, Dict, Optional, Callable
from concurrent.futures import ThreadPoolExecutor, as_completed

from db_cache import cache_db
from locales import get_text

try:
    import xxhash
    HAS_XXHASH = True
except ImportError:
    HAS_XXHASH = False

FILE_CATEGORIES = {
    "images": {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg", ".ico", ".tiff", ".heic", ".raw"},
    "videos": {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v", ".3gp", ".ts"},
    "audio": {".mp3", ".wav", ".flac", ".aac", ".ogg", ".m4a", ".wma", ".opus", ".alac"},
    "documents": {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".txt", ".rtf", ".odt", ".csv", ".epub"},
    "archives": {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".iso", ".dmg", ".cab"},
    "code": {".py", ".js", ".ts", ".jsx", ".tsx", ".html", ".css", ".json", ".xml", ".yaml", ".yml",
             ".cpp", ".c", ".h", ".hpp", ".cs", ".java", ".go", ".rs", ".php", ".rb", ".sql", ".sh", ".bat", ".ps1"}
}

def get_file_category(filename: str) -> str:
    ext = os.path.splitext(filename.lower())[1]
    for cat, exts in FILE_CATEGORIES.items():
        if ext in exts:
            return cat
    return "other"

def format_file_size(size_in_bytes: int) -> str:
    if size_in_bytes < 1024:
        return f"{size_in_bytes} B"
    elif size_in_bytes < 1024**2:
        return f"{size_in_bytes / 1024:.2f} KB"
    elif size_in_bytes < 1024**3:
        return f"{size_in_bytes / (1024**2):.2f} MB"
    else:
        return f"{size_in_bytes / (1024**3):.2f} GB"

def compute_wasted_bytes(groups: Dict[str, List["FileInfo"]]) -> int:
    """Bytes freed by keeping one file per group: sum of the actual duplicate
    sizes. (M9: ``(n-1) * group[0].size`` lies when group members differ in
    size — e.g. same-name matches or mixed-size clusters.)"""
    return sum(f.size for files in groups.values() for f in files[1:])

@dataclass
class FileInfo:
    path: str
    name: str
    size: int
    created: float
    modified: float
    hash: Optional[str] = None
    category: str = "other"
    ext: str = ""

    def __post_init__(self):
        if not self.ext:
            self.ext = os.path.splitext(self.name.lower())[1]
        if self.category == "other":
            self.category = get_file_category(self.name)

def get_file_hash(filepath: str, block_size: int = 65536) -> Optional[str]:
    """Calculates full SHA256 hash. Returns None on read error."""
    hasher = hashlib.sha256()
    try:
        with open(filepath, "rb") as f:
            for block in iter(lambda: f.read(block_size), b""):
                hasher.update(block)
        return hasher.hexdigest()
    except (OSError, PermissionError):
        return None

def get_turbo_hash(filepath: str, partial_size: int = 65536) -> Optional[str]:
    """Fast hash on first + last 64KB of file. Returns None on read error."""
    try:
        file_size = os.path.getsize(filepath)
        if HAS_XXHASH:
            hasher = xxhash.xxh64()
        else:
            hasher = hashlib.md5()

        with open(filepath, "rb") as f:
            hasher.update(f.read(partial_size))
            if file_size > partial_size * 2:
                f.seek(-partial_size, 2)
                hasher.update(f.read(partial_size))

        hasher.update(str(file_size).encode())
        return hasher.hexdigest()
    except (OSError, PermissionError):
        return None

# Known system locations matched as whole path components, never as substrings
# (M2: substring matching flagged paths like E:\bootcamp\notes.txt).
_ROOT_LEVEL_SYSTEM_DIRS = {
    "$recycle.bin", "system volume information", "$windows.~bt", "$windows.~ws",
    "recovery", "boot", "config.msi", "msocache", "windows",
    "program files", "program files (x86)", "programdata",
}
_SYSTEM_DIR_COMPONENTS = {"system32", "syswow64", "winsxs"}


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

def compare_byte_by_byte(file1: str, file2: str, buffer_size: int = 65536) -> bool:
    """Compares two files byte by byte. Returns True if identical."""
    try:
        with open(file1, 'rb') as f1, open(file2, 'rb') as f2:
            while True:
                b1 = f1.read(buffer_size)
                b2 = f2.read(buffer_size)
                if b1 != b2:
                    return False
                if not b1:
                    return True
    except (OSError, PermissionError):
        return False

def scan_directory(
    directories: List[str],
    by_name: bool = False,
    by_size: bool = True,
    by_hash: bool = True,
    by_byte: bool = False,
    progress_callback: Optional[Callable[[str, Optional[float]], None]] = None,
    cancel_flag: Optional[List[bool]] = None,
    exclude_patterns: Optional[List[str]] = None,
    turbo_mode: bool = True,
    min_size_bytes: int = 0,
    max_size_bytes: Optional[int] = None,
    ignore_empty_files: bool = True,
    use_cache: bool = True,
    max_workers: int = 8
) -> Dict[str, List[FileInfo]]:
    """
    High-performance multi-threaded duplicate scanner with SQLite persistent cache.
    """
    # All criteria off means "match nothing" — without this guard the scan
    # returned EVERY indexed file as one duplicate group, pre-selected for
    # deletion in the results UI.
    if not (by_name or by_size or by_hash or by_byte):
        raise ValueError("At least one comparison criterion (name/size/hash/bytes) must be enabled")

    def is_cancelled():
        return cancel_flag is not None and len(cancel_flag) > 0 and cancel_flag[0]

    def report_progress(msg: str, percent: Optional[float] = None):
        if progress_callback:
            progress_callback(msg, percent)

    def should_exclude(path: str) -> bool:
        if not exclude_patterns:
            return False
        path_norm = os.path.normpath(path).lower()
        # Match WHOLE path components only — a "Temp" chip must not swallow
        # D:\Templates or "attempt_final": substring matching silently shrank
        # scan results without any warning.
        components = path_norm.split(os.sep)
        for pattern in exclude_patterns:
            pat_lower = pattern.strip().lower()
            if not pat_lower:
                continue
            if any(fnmatch.fnmatch(component, pat_lower) for component in components):
                return True
        return False

    files_by_size = defaultdict(list)
    total_found = 0

    # Phase 1: File discovery & size indexing
    report_progress(get_text("scan_phase_indexing"), 0.0)
    for directory in directories:
        if is_cancelled():
            return {}
        if not os.path.exists(directory):
            continue

        for root, dirs, filenames in os.walk(directory, topdown=True, followlinks=False):
            if is_cancelled():
                return {}
            if exclude_patterns:
                dirs[:] = [d for d in dirs if not should_exclude(os.path.join(root, d))]

            for filename in filenames:
                filepath = os.path.join(root, filename)
                if should_exclude(filepath):
                    continue

                try:
                    stat = os.stat(filepath)
                    file_size = stat.st_size

                    if ignore_empty_files and file_size == 0:
                        continue
                    if file_size < min_size_bytes:
                        continue
                    if max_size_bytes is not None and file_size > max_size_bytes:
                        continue

                    info = FileInfo(
                        path=filepath,
                        name=filename,
                        size=file_size,
                        created=stat.st_ctime,
                        modified=stat.st_mtime
                    )
                    files_by_size[file_size].append(info)
                    total_found += 1

                    if total_found % 200 == 0:
                        report_progress(get_text("scan_indexed").format(total_found), None)
                except (OSError, PermissionError):
                    continue

    if is_cancelled():
        return {}

    # Filter out unique sizes
    candidate_groups = []
    if by_size:
        candidate_groups = [files for size, files in files_by_size.items() if len(files) > 1]
    else:
        all_candidates = []
        for flist in files_by_size.values():
            all_candidates.extend(flist)
        if len(all_candidates) > 1:
            candidate_groups = [all_candidates]

    total_candidates = sum(len(g) for g in candidate_groups)
    if total_candidates == 0:
        return {}

    if not by_hash and not by_name and not by_byte:
        return {str(g[0].size): g for g in candidate_groups}

    files_to_hash = []
    for g in candidate_groups:
        files_to_hash.extend(g)

    if by_hash:
        # Phase 2: Parallel hashing with SQLite Cache.
        # Skipped entirely for size/name-only scans — hashing every candidate there
        # is pure wasted I/O.
        report_progress(get_text("scan_phase_analyzing").format(total_candidates), 0.1)

        processed_count = 0

        def compute_hash_for_file(info: FileInfo) -> tuple[FileInfo, Optional[str]]:
            # Cancel must take effect BEFORE reading the file: queued tasks must
            # not start hashing multi-GB files after the user pressed Cancel.
            if is_cancelled():
                return info, None
            # 1. Try SQLite cache first
            if use_cache:
                cached_h = cache_db.get_file_hash(info.path, info.size, info.modified, turbo=turbo_mode)
                if cached_h:
                    return info, cached_h

            # 2. Compute hash
            if turbo_mode:
                h = get_turbo_hash(info.path)
                if h and use_cache:
                    cache_db.save_file_hash(info.path, info.size, info.modified, turbo_hash=h)
            else:
                h = get_file_hash(info.path)
                if h and use_cache:
                    cache_db.save_file_hash(info.path, info.size, info.modified, full_hash=h)
            return info, h

        # No `with` block: its __exit__ calls shutdown(wait=True) which JOINS
        # in-flight tasks — a cancelled scan would block for as long as the
        # currently-hashing files take (minutes for big files). On cancel we
        # return immediately; queued futures are dropped, running ones finish
        # in their own threads without holding the scan hostage.
        executor = ThreadPoolExecutor(max_workers=max_workers)
        futures = {executor.submit(compute_hash_for_file, info): info for info in files_to_hash}
        cancelled = False
        for future in as_completed(futures):
            if is_cancelled():
                cancelled = True
                break
            try:
                info, h = future.result()
                if h is not None:
                    info.hash = h
            except Exception:
                pass

            processed_count += 1
            if processed_count % 50 == 0 or processed_count == total_candidates:
                pct = 0.1 + (processed_count / total_candidates) * 0.7
                report_progress(get_text("scan_hashed").format(processed_count, total_candidates), pct)

        if cancelled:
            executor.shutdown(wait=False, cancel_futures=True)
            return {}
        executor.shutdown(wait=True)

    # Group by Key
    grouped = defaultdict(list)
    for info in files_to_hash:
        if by_hash and info.hash is None:
            continue

        key_parts = []
        if by_size:
            key_parts.append(str(info.size))
        if by_name:
            key_parts.append(info.name.lower())
        if by_hash:
            key_parts.append(info.hash)

        key = "|".join(key_parts)
        grouped[key].append(info)

    duplicates = {k: v for k, v in grouped.items() if len(v) > 1}

    # Turbo verification with full SHA-256 for collision elimination
    if turbo_mode and by_hash and duplicates:
        report_progress(get_text("scan_verifying_full"), 0.85)
        verified_grouped = defaultdict(list)

        for key, files in duplicates.items():
            if is_cancelled():
                return {}
            for info in files:
                full_h = cache_db.get_file_hash(info.path, info.size, info.modified, turbo=False) if use_cache else None
                if not full_h:
                    full_h = get_file_hash(info.path)
                    if full_h and use_cache:
                        cache_db.save_file_hash(info.path, info.size, info.modified, full_hash=full_h)

                if full_h is not None:
                    info.hash = full_h
                    v_key = f"{info.size}|{full_h}"
                    if by_name:
                        v_key = f"{info.name.lower()}|" + v_key
                    verified_grouped[v_key].append(info)

        duplicates = {k: v for k, v in verified_grouped.items() if len(v) > 1}

    # Phase 3: Byte-by-byte verification (if enabled)
    if by_byte and duplicates:
        report_progress(get_text("scan_phase_byte"), 0.9)
        final_duplicates = {}
        total_groups = len(duplicates)

        for g_idx, (key, files) in enumerate(duplicates.items(), 1):
            if is_cancelled():
                return {}

            remaining = files[:]
            sub_idx = 0
            while remaining:
                current = remaining.pop(0)
                matched = [current]
                unmatched = []
                for other in remaining:
                    if compare_byte_by_byte(current.path, other.path):
                        matched.append(other)
                    else:
                        unmatched.append(other)

                if len(matched) > 1:
                    final_duplicates[f"{key}_b{sub_idx}"] = matched
                    sub_idx += 1
                remaining = unmatched

            report_progress(get_text("scan_byte_progress").format(g_idx, total_groups), 0.9 + (g_idx / total_groups) * 0.1)

        duplicates = final_duplicates

    # Deterministic order inside every group: oldest file first.
    # The UI labels group[0] as "Original / Keep", so random arrival order
    # from the thread pool must not leak into the results.
    for files in duplicates.values():
        files.sort(key=lambda f: f.modified)

    report_progress(get_text("scan_complete"), 1.0)
    return duplicates

def scan_for_sample(
    sample_path: str,
    search_directories: List[str],
    by_name: bool = False,
    by_size: bool = True,
    by_hash: bool = True,
    by_byte: bool = False,
    progress_callback: Optional[Callable[[str, Optional[float]], None]] = None,
    cancel_flag: Optional[List[bool]] = None
) -> List[FileInfo]:
    """Finds duplicates of a specific sample file in the given directories."""
    # All criteria off would report every walked file as a "copy" of the sample.
    if not (by_name or by_size or by_hash or by_byte):
        raise ValueError("At least one comparison criterion (name/size/hash/bytes) must be enabled")
    if not os.path.exists(sample_path) or not os.path.isfile(sample_path):
        return []

    try:
        sample_stat = os.stat(sample_path)
        sample_size = sample_stat.st_size
        sample_name = os.path.basename(sample_path).lower()
        sample_hash = get_file_hash(sample_path) if (by_hash or by_byte) else None
        if by_hash and sample_hash is None:
            return []
    except (OSError, PermissionError):
        return []

    found_files = []
    total_scanned = 0
    sample_norm_path = os.path.normpath(sample_path).lower()

    for directory in search_directories:
        if not os.path.exists(directory):
            continue
        for root, _, filenames in os.walk(directory, followlinks=False):
            if cancel_flag and cancel_flag[0]:
                return found_files

            for filename in filenames:
                filepath = os.path.join(root, filename)
                if os.path.normpath(filepath).lower() == sample_norm_path:
                    continue

                total_scanned += 1
                if progress_callback and total_scanned % 100 == 0:
                    progress_callback(get_text("sample_scanned").format(total_scanned), None)

                try:
                    stat = os.stat(filepath)
                    if (by_size or by_hash or by_byte) and stat.st_size != sample_size:
                        # Hash or byte equality implies identical size, so
                        # wrong-size files are skipped before any hashing (M5).
                        continue
                    if by_name and filename.lower() != sample_name:
                        continue
                    if by_hash:
                        # SQLite cache first, like scan_directory does.
                        f_hash = cache_db.get_file_hash(filepath, stat.st_size, stat.st_mtime, turbo=False)
                        if not f_hash:
                            f_hash = get_file_hash(filepath)
                            if f_hash:
                                cache_db.save_file_hash(filepath, stat.st_size, stat.st_mtime, full_hash=f_hash)
                        if f_hash != sample_hash or f_hash is None:
                            continue
                    if by_byte:
                        if not compare_byte_by_byte(sample_path, filepath):
                            continue

                    found_files.append(FileInfo(
                        path=filepath,
                        name=filename,
                        size=stat.st_size,
                        created=stat.st_ctime,
                        modified=stat.st_mtime,
                        hash=sample_hash
                    ))
                except (OSError, PermissionError):
                    continue

    return found_files

def calculate_similarity(file1: str, file2: str) -> float:
    """Calculates similarity between two files without memory overflow."""
    import difflib
    MAX_TEXT_SIZE = 2 * 1024 * 1024

    try:
        size1 = os.path.getsize(file1)
        size2 = os.path.getsize(file2)
        if size1 == 0 and size2 == 0:
            return 1.0
        if size1 > MAX_TEXT_SIZE or size2 > MAX_TEXT_SIZE:
            return 1.0 if compare_byte_by_byte(file1, file2) else 0.0
    except (OSError, PermissionError):
        return 0.0

    def is_binary(path: str) -> bool:
        try:
            with open(path, 'rb') as check_file:
                chunk = check_file.read(1024)
                return b'\x00' in chunk
        except Exception:
            return True

    if is_binary(file1) or is_binary(file2):
        return 1.0 if compare_byte_by_byte(file1, file2) else 0.0

    try:
        with open(file1, 'r', encoding='utf-8', errors='ignore') as f1, \
             open(file2, 'r', encoding='utf-8', errors='ignore') as f2:
            lines1 = f1.readlines(MAX_TEXT_SIZE)
            lines2 = f2.readlines(MAX_TEXT_SIZE)
            return difflib.SequenceMatcher(None, lines1, lines2).ratio()
    except Exception:
        return 0.0

def compare_folders(folder_a: str, folder_b: str, progress_callback: Optional[Callable[[str], None]] = None) -> dict:
    """Compares two folders and returns statistics."""
    files_a = {}
    files_b = {}

    for root, _, filenames in os.walk(folder_a, followlinks=False):
        for filename in filenames:
            path = os.path.join(root, filename)
            try:
                rel_path = os.path.relpath(path, folder_a)
                stat = os.stat(path)
                files_a[rel_path] = FileInfo(path, filename, stat.st_size, stat.st_ctime, stat.st_mtime)
            except (OSError, PermissionError):
                continue

    for root, _, filenames in os.walk(folder_b, followlinks=False):
        for filename in filenames:
            path = os.path.join(root, filename)
            try:
                rel_path = os.path.relpath(path, folder_b)
                stat = os.stat(path)
                files_b[rel_path] = FileInfo(path, filename, stat.st_size, stat.st_ctime, stat.st_mtime)
            except (OSError, PermissionError):
                continue

    unique_a = []
    unique_b = []
    common = []

    all_keys = set(files_a.keys()) | set(files_b.keys())
    total = len(all_keys)
    processed = 0

    for rel_path in all_keys:
        processed += 1
        if progress_callback and processed % 20 == 0:
            progress_callback(get_text("compare_progress").format(processed, total))

        in_a = rel_path in files_a
        in_b = rel_path in files_b

        if in_a and not in_b:
            unique_a.append(files_a[rel_path])
        elif in_b and not in_a:
            unique_b.append(files_b[rel_path])
        else:
            fa = files_a[rel_path]
            fb = files_b[rel_path]
            similarity = 1.0
            if fa.size != fb.size:
                similarity = calculate_similarity(fa.path, fb.path)
            else:
                if not compare_byte_by_byte(fa.path, fb.path):
                    similarity = calculate_similarity(fa.path, fb.path)

            common.append({
                "file_a": fa,
                "file_b": fb,
                "similarity": similarity,
                "newer": "a" if fa.modified > fb.modified else "b" if fb.modified > fa.modified else "same",
                "larger": "a" if fa.size > fb.size else "b" if fb.size > fa.size else "same"
            })

    return {
        "unique_a": unique_a,
        "unique_b": unique_b,
        "common": common,
        "total_files": total
    }
