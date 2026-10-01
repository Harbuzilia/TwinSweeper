import os
import fnmatch
from typing import List, Dict, Tuple, Optional, Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from PIL import Image, ImageOps

from scanner import FileInfo, long_path
from fs_filters import InodeDeduper, prune_dirs
from db_cache import cache_db
from locales import get_text

# Formats PIL decodes out of the box (AVIF included — Pillow 12.3 ships the
# decoder). HEIC/RAW and friends need extra plugins — they are counted and
# reported instead of silently ignored (an iPhone library scanned with
# "0 groups found" is not "no duplicates").
IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".jfif", ".png", ".webp", ".bmp", ".gif",
    ".tiff", ".tif", ".ico", ".tga", ".ppm", ".pgm", ".pbm", ".pcx", ".avif",
}
UNSUPPORTED_IMAGE_EXTENSIONS = {
    ".heic", ".heif", ".cr2", ".nef", ".arw", ".dng",
    ".raw", ".orf", ".rw2", ".srw", ".raf",
}

def compute_dhash(image_path: str, hash_size: int = 8) -> Optional[str]:
    """
    Computes 64-bit difference hash (dHash) for an image.
    Resistant to scaling, compression artifacts, and color changes.
    """
    try:
        with Image.open(long_path(image_path)) as img:
            # EXIF orientation: phone photos are stored "sideways" plus an
            # orientation tag, while re-saved copies (messengers, editors) are
            # physically rotated. Without transposing to the canonical
            # orientation the same photo in both forms hashes apart.
            img = ImageOps.exif_transpose(img)
            # Convert to grayscale and resize to (hash_size + 1, hash_size)
            img = img.convert("L").resize((hash_size + 1, hash_size), Image.Resampling.BILINEAR)
            # tobytes() yields one byte per pixel for mode "L" without the
            # Image.getdata() API deprecated for removal in Pillow 14 (M4).
            pixels = img.tobytes()

            # Compare adjacent pixels in each row
            diff = []
            for row in range(hash_size):
                row_start = row * (hash_size + 1)
                for col in range(hash_size):
                    p_left = pixels[row_start + col]
                    p_right = pixels[row_start + col + 1]
                    diff.append(1 if p_left > p_right else 0)

            # Convert 64 bits to hex string
            decimal_val = 0
            for bit in diff:
                decimal_val = (decimal_val << 1) | bit
            return f"{decimal_val:016x}"
    except Exception:
        return None

def hamming_distance(hex1: str, hex2: str) -> int:
    """Calculates bitwise Hamming distance between two 64-bit hex hashes (0 to 64)."""
    try:
        val1 = int(hex1, 16)
        val2 = int(hex2, 16)
        return (val1 ^ val2).bit_count()
    except ValueError:
        return 64

def similarity_percentage(hex1: str, hex2: str) -> float:
    """Returns visual similarity percentage (0.0 to 100.0)."""
    dist = hamming_distance(hex1, hex2)
    return max(0.0, (1.0 - (dist / 64.0))) * 100.0

def scan_similar_images(
    directories: List[str],
    similarity_threshold: float = 0.90,  # 0.80 to 1.0 (e.g. 0.90 = 90%)
    progress_callback: Optional[Callable[[str, Optional[float]], None]] = None,
    cancel_flag: Optional[List[bool]] = None,
    exclude_patterns: Optional[List[str]] = None,
    max_workers: int = 8
) -> Dict[str, List[FileInfo]]:
    """
    Scans directories for visually similar images using perceptual hashing (pHash/dHash).
    Clusters matching images into groups.
    """
    def is_cancelled():
        return cancel_flag is not None and len(cancel_flag) > 0 and cancel_flag[0]

    def report(msg: str, pct: Optional[float] = None):
        if progress_callback:
            progress_callback(msg, pct)

    def should_exclude(path: str) -> bool:
        """Same matching rules as scanner.scan_directory: whole path components,
        or a normalized path prefix for patterns containing a separator."""
        if not exclude_patterns:
            return False
        path_norm = os.path.normpath(path).lower()
        components = path_norm.split(os.sep)
        for pattern in exclude_patterns:
            p = pattern.strip().lower()
            if not p:
                continue
            if os.sep in p or (os.altsep and os.altsep in p) or p.endswith(":"):
                pat_path = os.path.normpath(p)
                if path_norm == pat_path or path_norm.startswith(pat_path + os.sep):
                    return True
                continue
            if any(fnmatch.fnmatch(component, p) for component in components):
                return True
        return False

    # 1. Discover all image files
    report(get_text("phash_discovering"), 0.0)
    image_files: List[FileInfo] = []
    skipped_unsupported: set = set()
    # One physical image must never be indexed twice: a hardlink/junction
    # alias would appear as a "similar copy" of itself (M2/M3).
    inode_dedup = InodeDeduper()

    for directory in directories:
        if is_cancelled() or not os.path.exists(directory):
            continue
        for root, dirs, filenames in os.walk(directory, followlinks=False):
            if is_cancelled():
                return {}
            # Shared system-location/junction pruning (see fs_filters) so the
            # walker never enters them; user exclude patterns prune on top.
            prune_dirs(root, dirs)
            # Prune excluded directories so the walker never descends into them.
            if exclude_patterns:
                dirs[:] = [d for d in dirs if not should_exclude(os.path.join(root, d))]
            for filename in filenames:
                ext = os.path.splitext(filename.lower())[1]
                if ext not in IMAGE_EXTENSIONS:
                    if ext in UNSUPPORTED_IMAGE_EXTENSIONS:
                        skipped_unsupported.add(ext)
                    continue
                filepath = os.path.join(root, filename)
                if should_exclude(filepath):
                    continue
                try:
                    stat = os.stat(filepath)
                    # Skip an image already indexed under another path
                    # (hardlink / junction alias / overlapping root).
                    if inode_dedup.already_seen(stat):
                        continue
                    if stat.st_size > 0:
                        image_files.append(FileInfo(
                            path=filepath,
                            name=filename,
                            size=stat.st_size,
                            created=stat.st_ctime,
                            modified=stat.st_mtime,
                            category="images"
                        ))
                except (OSError, PermissionError):
                    continue

    total_images = len(image_files)

    # Report skipped formats BEFORE the early-return: a library of ONLY
    # HEIC/RAW files has total_images < 2 and would otherwise silently return
    # {} → "no duplicates found" with no explanation (round 3).
    if skipped_unsupported and not is_cancelled():
        report(get_text("phash_skipped_unsupported").format(', '.join(sorted(skipped_unsupported))), None)

    if total_images < 2 or is_cancelled():
        return {}

    report(get_text("phash_hashing").format(total_images), 0.1)

    # 2. Compute pHashes in parallel using SQLite Cache
    def get_or_calc_phash(info: FileInfo) -> Tuple[FileInfo, Optional[str]]:
        # Cancel takes effect before opening/decoding the image.
        if is_cancelled():
            return info, None
        # Check SQLite Cache first
        cached_phash = cache_db.get_image_phash(info.path, info.size, info.modified)
        if cached_phash:
            return info, cached_phash

        phash = compute_dhash(info.path)
        if phash:
            cache_db.save_image_phash(info.path, info.size, info.modified, phash)
        return info, phash

    hashed_images: List[Tuple[FileInfo, str]] = []
    processed = 0

    # No `with` block — see scanner.scan_directory: __exit__ would join
    # in-flight phash tasks and freeze a "cancelled" scan.
    executor = ThreadPoolExecutor(max_workers=max_workers)
    futures = {executor.submit(get_or_calc_phash, info): info for info in image_files}
    cancelled = False
    for future in as_completed(futures):
        if is_cancelled():
            cancelled = True
            break
        try:
            info, phash = future.result()
            if phash:
                info.hash = phash
                hashed_images.append((info, phash))
        except Exception:
            pass

        processed += 1
        if processed % 50 == 0 or processed == total_images:
            pct = 0.1 + (processed / total_images) * 0.6
            report(get_text("phash_hashed").format(processed, total_images), pct)

    if cancelled:
        executor.shutdown(wait=False, cancel_futures=True)
        return {}
    executor.shutdown(wait=True)

    if is_cancelled() or len(hashed_images) < 2:
        return {}

    # 3. Cluster by Hamming Distance using Disjoint Set (Union-Find)
    report(get_text("phash_clustering"), 0.75)
    max_hamming_dist = int(64 * (1.0 - similarity_threshold))

    parent = list(range(len(hashed_images)))

    def find_set(v):
        if v == parent[v]:
            return v
        parent[v] = find_set(parent[v])
        return parent[v]

    def union_sets(a, b):
        root_a = find_set(a)
        root_b = find_set(b)
        if root_a != root_b:
            parent[root_b] = root_a

    # Precompiled integer hashes: the pairwise loop is O(n^2) and re-parsing
    # hex strings + bin().count('1') for every pair dominated its cost (M4).
    hash_ints = [int(h, 16) for _, h in hashed_images]
    num_hashes = len(hashed_images)
    for i in range(num_hashes):
        if is_cancelled():
            return {}
        for j in range(i + 1, num_hashes):
            # Periodic cancel check keeps the hot loop tight yet responsive.
            if (j & 0x3FF) == 0 and is_cancelled():
                return {}
            if (hash_ints[i] ^ hash_ints[j]).bit_count() <= max_hamming_dist:
                union_sets(i, j)

    # 4. Group results — complete-linkage refinement (round 3).
    # Union-find is transitive: A~B and B~C merge A with C even when the A–C
    # distance is far beyond the threshold, so visually different photos end up
    # in one "similar" group (and the user trusts it when deleting). Split each
    # connected component into subclusters where EVERY pair is within the limit.
    from collections import defaultdict
    components = defaultdict(list)
    for idx in range(num_hashes):
        components[find_set(idx)].append(idx)

    results = {}
    group_index = 0
    for member_indices in components.values():
        # Greedy complete-linkage: place each image into the first subcluster
        # all of whose members are within the threshold, else start a new one.
        subclusters: List[List[int]] = []
        for idx in member_indices:
            h = hash_ints[idx]
            for sub in subclusters:
                if all((h ^ hash_ints[other]).bit_count() <= max_hamming_dist for other in sub):
                    sub.append(idx)
                    break
            else:
                subclusters.append([idx])
        for sub in subclusters:
            if len(sub) < 2:
                continue
            files = [hashed_images[i][0] for i in sub]
            # Sort files in group by modification date (oldest first)
            files.sort(key=lambda x: x.modified)
            group_index += 1
            # Numbered group key: two clusters whose first files share a name
            # must not overwrite each other in the dict (M3).
            results[f"Photo Group {group_index}: {files[0].name}"] = files

    report(get_text("scan_complete"), 1.0)
    return results
