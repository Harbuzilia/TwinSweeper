import os
import hashlib
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Callable, Dict, Iterable, Iterator, List, Optional, Tuple, TypeVar
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait

from app_logging import get_logger
from db_cache import cache_db
from fs_filters import (
    InodeDeduper,
    inode_key,
    is_system_path as is_system_path,  # re-export: results_view imports it from scanner (sweeper imports from fs_filters directly)
    prune_dirs,
    should_exclude,
)
from locales import get_text
from scan_index import open_scan_index

try:
    import xxhash
    HAS_XXHASH = True
except ImportError:
    HAS_XXHASH = False

logger = get_logger(__name__)

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

def long_path(path: str) -> str:
    r"""Windows \\?\ long-path prefix, applied ONLY when the path exceeds the
    legacy MAX_PATH (260) so normal paths stay byte-identical — zero regression
    risk for the common case. Lets os.open/stat/read reach files deeper than
    260 chars during SCANNING. Destructive operations deliberately keep the
    plain display path: their C2 os.stat then fails on a >260 target and the
    file is safely skipped rather than touched through an exotic path form."""
    if os.name != "nt" or len(path) <= 255:
        return path
    try:
        p = os.path.abspath(path)
    except Exception:
        return path
    if p.startswith("\\\\?\\"):
        return p
    if p.startswith("\\\\"):  # UNC \\server\share
        return "\\\\?\\UNC\\" + p[2:]
    return "\\\\?\\" + p

def get_file_hash(filepath: str, block_size: int = 65536) -> Optional[str]:
    """Calculates full SHA256 hash. Returns None on read error."""
    hasher = hashlib.sha256()
    try:
        with open(long_path(filepath), "rb") as f:
            for block in iter(lambda: f.read(block_size), b""):
                hasher.update(block)
        return hasher.hexdigest()
    except (OSError, PermissionError):
        return None

def get_turbo_hash(filepath: str, partial_size: int = 65536) -> Optional[str]:
    """Fast hash on first + last 64KB of file. Returns None on read error."""
    try:
        lp = long_path(filepath)
        file_size = os.path.getsize(lp)
        if HAS_XXHASH:
            hasher = xxhash.xxh64()
        else:
            hasher = hashlib.md5()

        with open(lp, "rb") as f:
            hasher.update(f.read(partial_size))
            if file_size > partial_size * 2:
                f.seek(-partial_size, 2)
                hasher.update(f.read(partial_size))

        hasher.update(str(file_size).encode())
        return hasher.hexdigest()
    except (OSError, PermissionError):
        return None

def verify_file_unchanged(path: str, expected_size: int, expected_mtime: float) -> Tuple[bool, str]:
    """C2 TOCTOU guard: confirms the file still matches its scan-time snapshot
    (size and mtime) before a destructive operation. Lives in scanner (the
    lowest-level module) so both main.py and the sweeper can share it without
    a circular import. Returns (True, "") or (False, human-readable reason)."""
    try:
        stat = os.stat(path)
    except OSError as ex:
        return False, get_text("verify_failed_access").format(ex)
    if stat.st_size != expected_size:
        return False, get_text("verify_failed_size").format(expected_size, stat.st_size)
    if abs(stat.st_mtime - expected_mtime) > 0.001:
        return False, get_text("verify_failed_mtime")
    return True, ""

# System-location matching, walk pruning and physical-file (inode) dedup now
# live in fs_filters.py — the shared rules every scanner must agree on.

def compare_byte_by_byte(file1: str, file2: str, buffer_size: int = 65536) -> bool:
    """Compares two files byte by byte. Returns True if identical."""
    try:
        with open(long_path(file1), 'rb') as f1, open(long_path(file2), 'rb') as f2:
            while True:
                b1 = f1.read(buffer_size)
                b2 = f2.read(buffer_size)
                if b1 != b2:
                    return False
                if not b1:
                    return True
    except (OSError, PermissionError):
        return False

# M6 byte-phase reference cache. A subgroup's reference is read ONCE and
# its content is compared against every candidate from RAM; 32 MB covers
# the common duplicate kinds (documents, photos, audio) while capping the
# scanner's transient memory. Oversized references fall back to streaming
# per-candidate comparison — bounded memory beats saved I/O for giant files.
BYTE_REFERENCE_CACHE_LIMIT = 32 * 1024 * 1024


def load_byte_reference(path: str, size: int) -> Optional[bytes]:
    """Reference content for one byte-phase subgroup, or None when it must
    not be cached (M6): an oversized reference would sit wholly in RAM, and
    an unreadable one cannot anchor any subgroup — both fall back to
    per-candidate streaming, which keeps the old open-failure behaviour
    (an unreadable reference matches nothing)."""
    if size > BYTE_REFERENCE_CACHE_LIMIT:
        return None
    try:
        # cap+1: a file that GREW past the cap since indexing must not be
        # slurped into memory whole here.
        with open(long_path(path), 'rb') as f:
            ref = f.read(BYTE_REFERENCE_CACHE_LIMIT + 1)
        # The read must match the INDEXED size: a file that changed on disk
        # since indexing would otherwise anchor the subgroup with content
        # shifted against the candidates' size-snapshots. Mismatch -> None
        # -> per-candidate streaming over the actual content.
        if len(ref) != size:
            return None
        return ref
    except (OSError, PermissionError):
        return None


def content_matches_bytes(expected: bytes, path: str, buffer_size: int = 65536) -> bool:
    """True when the file at ``path`` consists exactly of ``expected``.

    Chunked with early exit at the first difference, so a non-matching
    candidate usually costs one buffer instead of a full read — the
    reference content has already been paid for once (M6)."""
    try:
        with open(long_path(path), 'rb') as f:
            pos = 0
            while pos < len(expected):
                chunk = f.read(buffer_size)
                if not chunk or expected[pos:pos + len(chunk)] != chunk:
                    return False
                pos += len(chunk)
            # The candidate must end exactly where the reference ends.
            return f.read(1) == b""
    except (OSError, PermissionError):
        return False

# 2.2f memory ceiling: submitting every task at once keeps one Future object
# (plus its dict slot) per candidate alive for the whole hashing phase — on
# scans with hundreds of thousands of files that is hundreds of MB of pure
# bookkeeping. The bounded window keeps at most `window` futures pending and
# feeds new items only as earlier ones complete.
FUTURES_WINDOW_MIN = 64
FUTURES_WINDOW_PER_WORKER = 4

# Candidate rows are pulled from the discovery index and hashed in batches:
# a batch bounds how many FileInfo objects coexist during the hashing phase,
# while staying far above the worker count so the pool never starves (2.2f).
CANDIDATE_BATCH_FILES = 4096

_T = TypeVar("_T")
_R = TypeVar("_R")


def default_futures_window(max_workers: int) -> int:
    """Default pending-futures window: enough headroom to keep every worker
    busy (4 queued tasks per worker) without unbounded submission."""
    return max(FUTURES_WINDOW_MIN, max_workers * FUTURES_WINDOW_PER_WORKER)


def bounded_as_completed(
    executor: ThreadPoolExecutor,
    fn: Callable[[_T], _R],
    items: Iterable[_T],
    window: int,
    is_cancelled: Optional[Callable[[], bool]] = None,
) -> Iterator[Future]:
    """``as_completed`` with a bounded submission window (2.2f).

    Submits at most ``window`` tasks ahead of the consumer and refills only as
    futures complete, so peak memory is O(window) futures instead of O(len(items)).
    ``is_cancelled`` is checked BEFORE every submission and after every drained
    completion set: once it flips, no further items are fed and the generator
    returns, leaving any still-pending futures to the caller's
    ``executor.shutdown(wait=False, cancel_futures=True)`` — the established
    cancel contract of the scan phases.

    Yields futures in completion order; exceptions surface on ``future.result()``
    exactly as with ``as_completed``.
    """
    if window < 1:
        raise ValueError("window must be >= 1")
    cancelled = is_cancelled if is_cancelled is not None else (lambda: False)
    it = iter(items)
    pending: set = set()
    exhausted = False

    def _feed() -> bool:
        """Top the window up from the items iterator. Returns True when the
        cancel flag stopped the feeding (exhaustion sets `exhausted`)."""
        nonlocal exhausted
        while not exhausted and len(pending) < window:
            if cancelled():
                return True
            try:
                item = next(it)
            except StopIteration:
                exhausted = True
                break
            pending.add(executor.submit(fn, item))
        return False

    stopped = _feed()
    while pending and not stopped:
        done, pending = wait(pending, return_when=FIRST_COMPLETED)
        for future in done:
            yield future
        if cancelled():
            break
        stopped = _feed()

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
    max_workers: int = 8,
    futures_window: Optional[int] = None
) -> Dict[str, List[FileInfo]]:
    """
    High-performance multi-threaded duplicate scanner with SQLite persistent cache.

    ``futures_window`` (2.2f): cap on simultaneously pending hashing futures;
    None uses ``default_futures_window(max_workers)``. Lower values trade a
    little scheduling overhead for lower peak RAM.
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

    # User exclude patterns are matched by the shared fs_filters rules
    # (whole components / normalized path prefixes) — one canonical
    # implementation for every scanner.

    total_found = 0
    # One physical file must never appear twice in the results. Hardlinks,
    # junction/symlink aliases and overlapping scan roots (C:\ together with
    # C:\Users) all resolve to the same (device, inode); grouping such a pair
    # as "duplicates" would let the user delete a file and thereby destroy the
    # very "original" it pointed at (round 3).
    inode_dedup = InodeDeduper()
    # 2.2f memory ceiling: discovery rows spill to a throwaway SQLite index
    # instead of a FileInfo per walked file — unique-size files (the bulk of
    # a typical tree) never become objects, and candidate groups stream back
    # lazily in discovery order.
    index = open_scan_index()

    try:
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
                # System locations and junctions are pruned from every walk —
                # the shared rules live in fs_filters (M2/M3).
                prune_dirs(root, dirs)
                if exclude_patterns:
                    dirs[:] = [d for d in dirs if not should_exclude(os.path.join(root, d), exclude_patterns)]

                for filename in filenames:
                    filepath = os.path.join(root, filename)
                    if should_exclude(filepath, exclude_patterns):
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

                        # Skip a file we already indexed under another path
                        # (hardlink / junction alias / overlapping root).
                        if inode_dedup.already_seen(stat):
                            continue

                        index.add(filepath, filename, file_size, stat.st_ctime, stat.st_mtime)
                        total_found += 1

                        if total_found % 200 == 0:
                            report_progress(get_text("scan_indexed").format(total_found), None)
                    except (OSError, PermissionError):
                        continue

        if is_cancelled():
            return {}
        index.finish()

        # Unique sizes never leave the index: only duplicate-size groups (or,
        # with by_size off, the single combined pool) stream back out.
        num_groups, total_candidates = index.candidate_stats(by_size)
        if total_candidates == 0:
            return {}

        # Quick win: the user sees the scan's scale BEFORE the expensive
        # hashing phase starts.
        report_progress(get_text("scan_candidates_found").format(num_groups, total_candidates), 0.05)

        if not by_hash and not by_name and not by_byte:
            return {
                str(rows[0][2]): [FileInfo(path=p, name=n, size=s, created=c, modified=m) for p, n, s, c, m in rows]
                for rows in index.iter_candidate_groups(by_size)
            }

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

        duplicates: Dict[str, List[FileInfo]] = {}
        cancelled = False
        executor: Optional[ThreadPoolExecutor] = None
        hash_started = time.monotonic()
        window = futures_window if futures_window is not None else default_futures_window(max_workers)

        def hash_batch(batch: List[FileInfo]) -> bool:
            """Hash one batch through the bounded futures window (2.2f: at
            most `window` futures pending instead of one per candidate).
            Returns True when the scan was cancelled mid-phase."""
            nonlocal processed_count, executor
            if executor is None:
                # No `with` block: its __exit__ calls shutdown(wait=True) which JOINS
                # in-flight tasks — a cancelled scan would block for as long as the
                # currently-hashing files take (minutes for big files). On cancel we
                # return immediately; queued futures are dropped, running ones finish
                # in their own threads without holding the scan hostage.
                executor = ThreadPoolExecutor(max_workers=max_workers)
            for future in bounded_as_completed(executor, compute_hash_for_file, batch, min(window, len(batch)), is_cancelled):
                if is_cancelled():
                    return True
                try:
                    info, h = future.result()
                    if h is not None:
                        info.hash = h
                except Exception:
                    pass

                processed_count += 1
                if processed_count % 50 == 0 or processed_count == total_candidates:
                    pct = 0.1 + (processed_count / total_candidates) * 0.7
                    msg = get_text("scan_hashed").format(processed_count, total_candidates)
                    elapsed = time.monotonic() - hash_started
                    if elapsed >= 1.0 and processed_count > 0:
                        # Quick win: live speed + ETA instead of a bare counter.
                        speed = processed_count / elapsed
                        eta = (total_candidates - processed_count) / speed
                        msg += get_text("scan_eta_suffix").format(int(speed), int(eta))
                    report_progress(msg, pct)
            return False

        def group_batch(batch: List[FileInfo]) -> None:
            """Group one batch by key. With by_size the key contains the size
            and a size group is never split across batches, so per-batch
            grouping is identical to the old whole-list grouping."""
            grouped = defaultdict(list)
            for info in batch:
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

            for key, files in grouped.items():
                if len(files) > 1:
                    duplicates[key] = files

        # Stream the candidate groups and hash them in bounded batches: peak
        # RAM stays O(batch + window) FileInfo objects instead of O(all
        # candidates) + one Future each (2.2f).
        batch: List[FileInfo] = []
        for rows in index.iter_candidate_groups(by_size):
            if is_cancelled():
                cancelled = True
                break
            # L4: CANDIDATE_BATCH_FILES is a RAM target, not a hard ceiling —
            # a size group is materialised and hashed whole, or same-content
            # members would land in different batches and the group would
            # shatter. A group bigger than the target therefore overshoots it
            # (the batch was below the target before the extend, so the group
            # alone decides); log the overshoot instead of letting the memory
            # peak silently double.
            if by_size and len(rows) > CANDIDATE_BATCH_FILES:
                logger.warning(
                    "size group of %d files exceeds batch size %d; hashing it as one oversized batch (the group is never split)",
                    len(rows),
                    CANDIDATE_BATCH_FILES,
                )
            batch.extend(FileInfo(path=p, name=n, size=s, created=c, modified=m) for p, n, s, c, m in rows)
            if len(batch) >= CANDIDATE_BATCH_FILES:
                if by_hash:
                    cancelled = hash_batch(batch)
                    if cancelled:
                        break
                group_batch(batch)
                batch = []
        if not cancelled and batch:
            if by_hash:
                cancelled = hash_batch(batch)
            if not cancelled:
                group_batch(batch)

        if cancelled or is_cancelled():
            if executor is not None:
                executor.shutdown(wait=False, cancel_futures=True)
            return {}
        if executor is not None:
            executor.shutdown(wait=True)

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

        # Phase 3: Byte-by-byte verification — byte-only mode (M6).
        # With by_hash on, every group reaching this point is already proven by
        # a FULL SHA-256: full mode hashes with SHA-256 directly, and the turbo
        # verification block above has replaced every partial hash with a full
        # one. Re-checking those groups byte-by-byte would repeat the same proof
        # at ~n times the I/O. A SHA-256 collision between same-sized files is
        # outside this project's risk model (README: full SHA-256 IS the
        # duplicate-proof standard), so the byte pass runs only when the user
        # asked for content proof WITHOUT hashing.
        if by_byte and duplicates and not by_hash:
            report_progress(get_text("scan_phase_byte"), 0.9)
            final_duplicates = {}
            total_groups = len(duplicates)

            for g_idx, (key, files) in enumerate(duplicates.items(), 1):
                if is_cancelled():
                    return {}

                remaining = deque(files)
                sub_idx = 0
                while remaining:
                    # Cancel must be honored INSIDE the subgroup loop too — a
                    # single huge group used to keep comparing (and reading)
                    # long after the user pressed Cancel (stage-1 review).
                    if is_cancelled():
                        return {}
                    current = remaining.popleft()
                    # M6: the reference is read once per subgroup and served
                    # from RAM to every candidate (see load_byte_reference for
                    # the oversized fallback).
                    ref_content = load_byte_reference(current.path, current.size)
                    matched = [current]
                    unmatched = deque()
                    for other in remaining:
                        # Equal content implies equal size — skip the I/O for
                        # size-mismatched candidates outright.
                        if other.size != current.size:
                            unmatched.append(other)
                            continue
                        if ref_content is not None:
                            is_same = content_matches_bytes(ref_content, other.path)
                        else:
                            is_same = compare_byte_by_byte(current.path, other.path)
                        if is_same:
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
    finally:
        # 2.2f: the spill DB is scan-scoped state — released on every exit
        # path (success, cancel, exception) instead of waiting for GC.
        index.close()

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
    # Exclude the sample's OWN aliases (hardlink / junction) by physical file
    # identity, not just by path string — otherwise a reported "copy" could be
    # the sample itself under another path, and deleting it destroys the sample.
    sample_key = inode_key(sample_stat)
    # M6 parity with scan_directory's byte phase: with by_hash on, a full
    # SHA-256 already proves content equality (a collision between
    # same-sized files is outside the project's risk model), so the byte
    # pass runs only as the content proof WITHOUT hashing — never as a
    # second, ~n-times-I/O proof of the same fact. In byte-only mode the
    # sample's content is read ONCE into RAM (up to
    # BYTE_REFERENCE_CACHE_LIMIT) and every candidate is verified against
    # it; an oversized or unreadable sample falls back to pairwise
    # streaming, preserving the old behaviour.
    verify_by_byte = by_byte and not by_hash
    sample_ref = load_byte_reference(sample_path, sample_size) if verify_by_byte else None

    for directory in search_directories:
        if not os.path.exists(directory):
            continue
        for root, dirs, filenames in os.walk(directory, followlinks=False):
            if cancel_flag and cancel_flag[0]:
                return found_files
            # Same pruning as scan_directory (see fs_filters): never index
            # system locations or descend into junctions (double-count /
            # infinite loop).
            prune_dirs(root, dirs)

            for filename in filenames:
                filepath = os.path.join(root, filename)
                if os.path.normpath(filepath).lower() == sample_norm_path:
                    continue

                total_scanned += 1
                if progress_callback and total_scanned % 100 == 0:
                    progress_callback(get_text("sample_scanned").format(total_scanned), None)

                try:
                    stat = os.stat(filepath)
                    # Skip the sample's own hardlink/junction alias.
                    if sample_key is not None and inode_key(stat) == sample_key:
                        continue
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
                    if verify_by_byte:
                        # RAM reference when the sample fits the M6 cache cap,
                        # pairwise streaming otherwise (or when it changed on
                        # disk since the stat above — load_byte_reference
                        # re-verifies the size it read).
                        if sample_ref is not None:
                            if not content_matches_bytes(sample_ref, filepath):
                                continue
                        elif not compare_byte_by_byte(sample_path, filepath):
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
                # Windows paths are case-insensitive: without normcase,
                # Report.PDF in A and report.pdf in B read as two different
                # files — each "unique" on its own side.
                files_a[os.path.normcase(rel_path)] = FileInfo(path, filename, stat.st_size, stat.st_ctime, stat.st_mtime)
            except (OSError, PermissionError):
                continue

    for root, _, filenames in os.walk(folder_b, followlinks=False):
        for filename in filenames:
            path = os.path.join(root, filename)
            try:
                rel_path = os.path.relpath(path, folder_b)
                stat = os.stat(path)
                files_b[os.path.normcase(rel_path)] = FileInfo(path, filename, stat.st_size, stat.st_ctime, stat.st_mtime)
            except (OSError, PermissionError):
                continue

    unique_a = []
    unique_b = []
    common = []

    all_keys = set(files_a.keys()) | set(files_b.keys())
    total = len(all_keys)
    processed = 0

    for rel_key in all_keys:
        processed += 1
        if progress_callback and processed % 20 == 0:
            progress_callback(get_text("compare_progress").format(processed, total))

        in_a = rel_key in files_a
        in_b = rel_key in files_b

        if in_a and not in_b:
            unique_a.append(files_a[rel_key])
        elif in_b and not in_a:
            unique_b.append(files_b[rel_key])
        else:
            fa = files_a[rel_key]
            fb = files_b[rel_key]
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
