import os
import hashlib
import fnmatch
from collections import defaultdict
from dataclasses import dataclass
from typing import List, Dict, Optional, Callable
from concurrent.futures import ThreadPoolExecutor, as_completed

from db_cache import cache_db

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

def is_system_path(path: str) -> bool:
    """Checks if the path belongs to a protected Windows system directory or file."""
    path_lower = os.path.normpath(path).lower()
    
    system_dirs = [
        r"c:\windows",
        r"c:\program files",
        r"c:\program files (x86)",
        r"c:\programdata\microsoft",
        r"c:\recovery",
        r"\system32",
        r"\syswow64",
        r"\winsxs",
        r"\boot",
        r"\system volume information",
        r"\$recycle.bin",
        r"\$windows.~bt",
        r"\$windows.~ws"
    ]
    
    for sys_dir in system_dirs:
        if sys_dir in path_lower:
            return True
            
    system_exts = {".sys", ".dll", ".inf", ".ocx", ".vxd", ".drv", ".cpl", ".rom", ".efi"}
    ext = os.path.splitext(path_lower)[1]
    if ext in system_exts:
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
    def is_cancelled():
        return cancel_flag is not None and len(cancel_flag) > 0 and cancel_flag[0]

    def report_progress(msg: str, percent: Optional[float] = None):
        if progress_callback:
            progress_callback(msg, percent)

    def should_exclude(path: str) -> bool:
        if not exclude_patterns:
            return False
        path_norm = os.path.normpath(path).lower()
        base_name = os.path.basename(path_norm)
        for pattern in exclude_patterns:
            pat_lower = pattern.strip().lower()
            if not pat_lower:
                continue
            if fnmatch.fnmatch(path_norm, f"*{pat_lower}*") or fnmatch.fnmatch(base_name, pat_lower):
                return True
        return False

    files_by_size = defaultdict(list)
    total_found = 0

    # Phase 1: File discovery & size indexing
    report_progress("Phase 1/3: Indexing files...", 0.0)
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
                        report_progress(f"Indexed {total_found} files...", None)
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
        report_progress(f"Phase 2/3: Analyzing {total_candidates} candidate files...", 0.1)

        processed_count = 0

        def compute_hash_for_file(info: FileInfo) -> tuple[FileInfo, Optional[str]]:
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

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(compute_hash_for_file, info): info for info in files_to_hash}
            for future in as_completed(futures):
                if is_cancelled():
                    executor.shutdown(wait=False, cancel_futures=True)
                    return {}
                try:
                    info, h = future.result()
                    if h is not None:
                        info.hash = h
                except Exception:
                    pass

                processed_count += 1
                if processed_count % 50 == 0 or processed_count == total_candidates:
                    pct = 0.1 + (processed_count / total_candidates) * 0.7
                    report_progress(f"Hashed {processed_count}/{total_candidates} files...", pct)

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
        report_progress("Verifying full hashes for potential matches...", 0.85)
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
        report_progress("Phase 3/3: Performing byte-by-byte verification...", 0.9)
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
                
            report_progress(f"Byte verification {g_idx}/{total_groups}...", 0.9 + (g_idx / total_groups) * 0.1)
            
        duplicates = final_duplicates

    # Deterministic order inside every group: oldest file first.
    # The UI labels group[0] as "Original / Keep", so random arrival order
    # from the thread pool must not leak into the results.
    for files in duplicates.values():
        files.sort(key=lambda f: f.modified)

    report_progress("Scan complete!", 1.0)
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
                    progress_callback(f"Scanned {total_scanned} files...", None)

                try:
                    stat = os.stat(filepath)
                    if by_size and stat.st_size != sample_size:
                        continue
                    if by_name and filename.lower() != sample_name:
                        continue
                    if by_hash:
                        f_hash = get_file_hash(filepath)
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
            progress_callback(f"Comparing {processed}/{total} files...")

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
