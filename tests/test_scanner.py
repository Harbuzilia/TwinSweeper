"""Tests for scanner.py — hashing, grouping, filters, turbo verification."""
import builtins
import hashlib
import os
import threading
import time

import pytest

import scanner
from scanner import (
    BYTE_REFERENCE_CACHE_LIMIT,
    FileInfo,
    calculate_similarity,
    compare_byte_by_byte,
    compare_folders,
    content_matches_bytes,
    format_file_size,
    get_file_category,
    get_file_hash,
    get_turbo_hash,
    is_system_path,
    load_byte_reference,
    scan_directory,
    scan_for_sample,
)


class TestFileInfo:
    def test_ext_derived_lowercase_from_name(self):
        info = FileInfo("C:\\Photo.JPG", "Photo.JPG", 10, 0.0, 0.0)
        assert info.ext == ".jpg"

    def test_category_derived_from_name_when_default(self):
        info = FileInfo("C:\\song.mp3", "song.mp3", 10, 0.0, 0.0)
        assert info.category == "audio"

    def test_explicit_category_is_preserved(self):
        info = FileInfo("C:\\song.mp3", "song.mp3", 10, 0.0, 0.0, category="images")
        assert info.category == "images"

    def test_unknown_extension_maps_to_other(self):
        info = FileInfo("C:\\data.xyz", "data.xyz", 10, 0.0, 0.0)
        assert info.category == "other"


class TestCategories:
    @pytest.mark.parametrize("name,expected", [
        ("photo.jpg", "images"),
        ("PHOTO.PNG", "images"),
        ("clip.MP4", "videos"),
        ("track.flac", "audio"),
        ("report.pdf", "documents"),
        ("archive.zip", "archives"),
        ("main.py", "code"),
        ("unknown.zzz", "other"),
    ])
    def test_get_file_category(self, name, expected):
        assert get_file_category(name) == expected


class TestFormatFileSize:
    @pytest.mark.parametrize("size,expected", [
        (512, "512 B"),
        (1024, "1.00 KB"),
        (1536, "1.50 KB"),
        (1024**2, "1.00 MB"),
        (1024**3, "1.00 GB"),
        (2 * 1024**3, "2.00 GB"),
    ])
    def test_format_file_size(self, size, expected):
        assert format_file_size(size) == expected


class TestHashing:
    def test_get_file_hash_matches_hashlib(self, tmp_path):
        p = tmp_path / "f.bin"
        content = b"twinsweeper test content" * 100
        p.write_bytes(content)
        expected = hashlib.sha256(content).hexdigest()
        assert get_file_hash(str(p)) == expected

    def test_get_file_hash_missing_file_returns_none(self, tmp_path):
        assert get_file_hash(str(tmp_path / "missing.bin")) is None

    def test_long_path_beyond_max_path_hashed(self, tmp_path):
        r"""Round 3 D: a file deeper than the legacy 260-char MAX_PATH must
        still be hashable — get_file_hash applies the \\?\ prefix internally.
        Without it, open() on the plain long path fails and returns None."""
        deep = tmp_path
        for _ in range(20):
            deep = deep / ("d" * 12)
        long_file = deep / "file.bin"
        assert len(str(long_file)) > 260
        # Create it through the prefixed form so Windows permits the deep path.
        try:
            os.makedirs("\\\\?\\" + str(deep), exist_ok=True)
            with open("\\\\?\\" + str(long_file), "wb") as fh:
                fh.write(b"LONG PATH CONTENT" * 10)
        except OSError:
            pytest.skip("this system cannot create >260-char paths")

        content = b"LONG PATH CONTENT" * 10
        # The plain (unprefixed) long path must hash via the internal prefix.
        assert get_file_hash(str(long_file)) == hashlib.sha256(content).hexdigest()
        assert get_turbo_hash(str(long_file)) is not None

    def test_turbo_hash_stable_and_size_sensitive(self, tmp_path):
        a = tmp_path / "a.bin"
        b = tmp_path / "b.bin"
        a.write_bytes(b"Y" * 300000)
        b.write_bytes(b"Y" * 300000)
        assert get_turbo_hash(str(a)) == get_turbo_hash(str(b))

        c = tmp_path / "c.bin"
        c.write_bytes(b"Y" * 300001)  # different size → different turbo hash
        assert get_turbo_hash(str(a)) != get_turbo_hash(str(c))

    def test_turbo_hash_missing_file_returns_none(self, tmp_path):
        assert get_turbo_hash(str(tmp_path / "missing.bin")) is None

    def test_compare_byte_by_byte(self, tmp_path):
        a, b, c = tmp_path / "a", tmp_path / "b", tmp_path / "c"
        a.write_bytes(b"same" * 10)
        b.write_bytes(b"same" * 10)
        c.write_bytes(b"same" * 9 + b"diff")
        assert compare_byte_by_byte(str(a), str(b)) is True
        assert compare_byte_by_byte(str(a), str(c)) is False


class TestIsSystemPath:
    """Regression tests for M2: substring matching caused false positives."""

    @pytest.mark.parametrize("path", [
        r"C:\Windows\System32\drivers\etc\hosts",
        r"C:\WINDOWS\explorer.exe",
        r"C:\Program Files\App\app.dll",
        r"c:\program files (x86)\App\app.dll",
        r"C:\Recovery\xyz",
        r"D:\$RECYCLE.BIN\S-1-5\x",
        r"E:\System Volume Information\x",
        r"C:\Boot\bootmgr",
        r"C:\$Windows.~BT\x",
    ])
    def test_real_system_paths_are_flagged(self, path):
        assert is_system_path(path) is True

    @pytest.mark.parametrize("path", [
        r"E:\AllMyProject\bootcamp\notes.txt",      # \boot substring bug
        r"E:\backup\system32copy\readme.txt",
        r"E:\winsxs_backup\old\file.txt",
        r"E:\MyPhotos\desktop.ini",
        r"E:\Games\mod.dll",                        # user-owned DLL
        r"E:\tools\driver.sys",
    ])
    def test_user_paths_with_system_like_substrings_are_not_flagged(self, path):
        assert is_system_path(path) is False

    def test_system_root_env_var_is_respected(self, tmp_path, monkeypatch):
        fake_root = tmp_path / "FakeWindows"
        fake_root.mkdir()
        monkeypatch.setenv("SystemRoot", str(fake_root))
        assert is_system_path(str(fake_root / "system32" / "x.dll")) is True
        assert is_system_path(str(fake_root / "normal.txt")) is True

    def test_program_data_microsoft_is_flagged(self, monkeypatch):
        monkeypatch.setenv("ProgramData", r"C:\ProgramData")
        assert is_system_path(r"C:\ProgramData\Microsoft\Windows\whatever") is True


class TestScanDirectory:
    def test_finds_duplicate_group(self, dup_tree):
        results = scan_directory([str(dup_tree)])
        assert len(results) == 1
        (files,) = results.values()
        assert len(files) == 3
        assert all(f.name == "dup.bin" for f in files)

    def test_unique_files_not_grouped(self, dup_tree):
        results = scan_directory([str(dup_tree)])
        all_paths = {f.path for files in results.values() for f in files}
        assert str(dup_tree / "a.txt") not in all_paths
        assert str(dup_tree / "other.log") not in all_paths

    def test_group_sorted_oldest_modified_first(self, dup_tree):
        # Make dir2's copy the oldest, dir1's the newest.
        now = time.time()
        os.utime(str(dup_tree / "dir2" / "dup.bin"), (now - 1000, now - 1000))
        os.utime(str(dup_tree / "dir1" / "dup.bin"), (now, now))
        results = scan_directory([str(dup_tree)])
        (files,) = results.values()
        assert files[0].path == str(dup_tree / "dir2" / "dup.bin")

    def test_by_name_only_groups_same_name(self, tmp_path):
        (tmp_path / "a" ).mkdir()
        (tmp_path / "b" ).mkdir()
        (tmp_path / "a" / "same.txt").write_text("one")
        (tmp_path / "b" / "same.txt").write_text("two")  # different content
        results = scan_directory([str(tmp_path)], by_name=True, by_hash=False, by_size=False)
        assert len(results) == 1
        assert len(next(iter(results.values()))) == 2

    def test_by_size_only_groups_same_size(self, dup_tree):
        results = scan_directory([str(dup_tree)], by_hash=False, by_byte=False)
        groups = [g for g in results.values() if len(g) > 1]
        assert len(groups) == 1  # the three dup.bin files
        assert len(groups[0]) == 3

    def test_min_size_filter(self, dup_tree):
        results = scan_directory([str(dup_tree)], min_size_bytes=2048)
        assert results == {}

    def test_max_size_filter(self, dup_tree):
        results = scan_directory([str(dup_tree)], max_size_bytes=512)
        assert results == {}

    def test_empty_files_ignored_by_default(self, tmp_path):
        (tmp_path / "x").mkdir()
        (tmp_path / "y").mkdir()
        (tmp_path / "x" / "e.bin").write_bytes(b"")
        (tmp_path / "y" / "e.bin").write_bytes(b"")
        assert scan_directory([str(tmp_path)]) == {}

    def test_empty_files_grouped_when_enabled(self, tmp_path):
        (tmp_path / "x").mkdir()
        (tmp_path / "y").mkdir()
        (tmp_path / "x" / "e.bin").write_bytes(b"")
        (tmp_path / "y" / "e.bin").write_bytes(b"")
        results = scan_directory([str(tmp_path)], ignore_empty_files=False)
        assert any(len(files) > 1 for files in results.values())

    def test_exclude_patterns_prune_directories(self, dup_tree):
        results = scan_directory([str(dup_tree)], exclude_patterns=["dir2"])
        (files,) = results.values()
        assert len(files) == 2
        assert not any("dir2" in f.path for f in files)

    def test_cancel_flag_returns_empty(self, dup_tree):
        assert scan_directory([str(dup_tree)], cancel_flag=[True]) == {}

    def test_nonexistent_directory_is_skipped(self, tmp_path):
        assert scan_directory([str(tmp_path / "no_such_dir")]) == {}

    def test_second_scan_is_served_from_cache(self, dup_tree, monkeypatch):
        """The cache's whole point: an unchanged tree must NOT be re-hashed.
        Proven by counting real hash invocations on the second scan — the old
        test only compared results and passed even with a dead cache."""
        first = scan_directory([str(dup_tree)])
        assert first  # sanity: duplicates found and cached

        hash_calls = []
        real_turbo = scanner.get_turbo_hash
        real_full = scanner.get_file_hash
        monkeypatch.setattr(
            scanner, "get_turbo_hash",
            lambda p, *a, **k: hash_calls.append(("turbo", p)) or real_turbo(p, *a, **k),
        )
        monkeypatch.setattr(
            scanner, "get_file_hash",
            lambda p, *a, **k: hash_calls.append(("full", p)) or real_full(p, *a, **k),
        )

        second = scan_directory([str(dup_tree)])

        assert {k: [f.path for f in v] for k, v in second.items()} == \
               {k: [f.path for f in v] for k, v in first.items()}
        assert hash_calls == []  # zero re-hashing — everything came from SQLite

    def test_cache_invalidated_after_file_change(self, dup_tree):
        scan_directory([str(dup_tree)])
        # Rewrite two of the three duplicates with different content
        # → no same-content pair remains → group must dissolve.
        (dup_tree / "dir2" / "dup.bin").write_bytes(b"Z" * 1024)
        (dup_tree / "dir3" / "dup.bin").write_bytes(b"Y" * 1024)
        results = scan_directory([str(dup_tree)])
        assert results == {}

    def test_edited_file_not_grouped_via_stale_cache(self, tmp_path):
        """Round 3 CRITICAL regression — the user's exact fear.

        A file edited IN PLACE (same total size, same first/last 64KB so the
        turbo hash is unchanged, new mtime) must NOT be reported as a duplicate
        of its own OLD content. Before the fix, saving the new turbo hash kept
        the stale full_hash alive (COALESCE), so the SHA-256 verification phase
        'confirmed' the edited file as a duplicate and preselected it for
        deletion — destroying unique data.
        """
        block = 65536
        head, tail = b"H" * block, b"T" * block
        a = tmp_path / "a.bin"
        b = tmp_path / "b.bin"
        a.write_bytes(head + b"MID-VERSION-1" + tail)
        b.write_bytes(head + b"MID-VERSION-1" + tail)  # identical to a v1

        # Scan 1: a and b are genuine duplicates; both full hashes get cached.
        first = scan_directory([str(tmp_path)], by_hash=True, turbo_mode=True, use_cache=True)
        assert len(first) == 1 and len(next(iter(first.values()))) == 2

        # Edit a's middle in place: same size, same head/tail (turbo hash
        # unchanged), different content. Bump mtime so the cache notices.
        a.write_bytes(head + b"MID-VERSION-2" + tail)
        assert a.stat().st_size == b.stat().st_size  # size unchanged on purpose
        future = time.time() + 10
        os.utime(str(a), (future, future))

        # Scan 2: a(v2) and b differ in the middle → full hashes differ →
        # they must NOT be grouped. A stale cache would wrongly group them.
        second = scan_directory([str(tmp_path)], by_hash=True, turbo_mode=True, use_cache=True)
        assert second == {}

    def test_turbo_collision_split_by_full_hash(self, tmp_path):
        """Two files with identical first+last 64KB but different middle:
        turbo hash collides, full SHA-256 verification must split them."""
        block = 65536
        a = tmp_path / "a.bin"
        b = tmp_path / "b.bin"
        a.write_bytes(b"A" * block + b"UNIQUE-A" + b"A" * block)
        b.write_bytes(b"A" * block + b"UNIQUE-B" + b"A" * block)
        # Sanity: turbo hashes collide, full hashes differ.
        assert get_turbo_hash(str(a)) == get_turbo_hash(str(b))
        assert get_file_hash(str(a)) != get_file_hash(str(b))

        results = scan_directory([str(tmp_path)], turbo_mode=True)
        assert results == {}

    def test_byte_by_byte_verification(self, tmp_path):
        block = 65536
        a = tmp_path / "a.bin"
        b = tmp_path / "b.bin"
        a.write_bytes(b"A" * block + b"MID-A" + b"A" * block)
        b.write_bytes(b"A" * block + b"MID-B" + b"A" * block)
        results = scan_directory([str(tmp_path)], by_byte=True)
        assert results == {}


class TestBytePhaseM6:
    """M6: the byte phase must read each file's content once (no reference
    re-reads per candidate) and must not re-prove with bytes what a full
    SHA-256 has already proven."""

    def _count_binary_opens(self, monkeypatch, paths):
        """Patches builtins.open, recording every binary-mode open of the
        given files — the honest I/O unit for the byte phase, independent of
        which internal function performs the comparison."""
        watch = {os.path.normcase(p) for p in paths}
        opens = []
        real_open = builtins.open

        def counting_open(file, mode="r", *args, **kwargs):
            if "b" in mode:
                norm = os.path.normcase(os.fspath(file))
                if norm in watch:
                    opens.append(norm)
            return real_open(file, mode, *args, **kwargs)

        monkeypatch.setattr(builtins, "open", counting_open)
        return opens

    def test_byte_only_reads_group_files_once(self, tmp_path, monkeypatch):
        """M6 regression: in a byte-only group of n identical files the old
        phase opened the reference once per candidate — 2(n-1) opens for
        n=5. Contract now: each file is opened exactly once."""
        content = b"M6 reference content" * 512
        paths = []
        for i in range(5):
            p = tmp_path / f"g{i}.bin"
            p.write_bytes(content)
            paths.append(str(p))

        opens = self._count_binary_opens(monkeypatch, paths)

        results = scan_directory([str(tmp_path)], by_hash=False, by_byte=True, use_cache=False)

        (group,) = results.values()
        assert {f.path for f in group} == set(paths)
        # Every file really compared (guards against a fake shortcut)...
        assert set(opens) == {os.path.normcase(p) for p in paths}
        # ...and each at most once: the old code made 8 opens out of 5 files.
        assert len(opens) <= 5

    @pytest.mark.parametrize("turbo_mode,expected_opens", [(True, 4), (False, 2)])
    def test_hash_and_byte_does_not_reprove_with_bytes(self, tmp_path, monkeypatch, turbo_mode, expected_opens):
        """M6 semantics: with by_hash on, groups arrive already proven by a
        full SHA-256 (turbo verification replaces partial hashes before
        Phase 3), so the byte phase must not open the files again. Expected
        opens = hashing only: one full-SHA256 pass per file, plus one
        partial pass per file in turbo mode. The old byte pass added 2."""
        content = b"M6 proof content" * 512
        paths = []
        for name in ("p0.bin", "p1.bin"):
            p = tmp_path / name
            p.write_bytes(content)
            paths.append(str(p))

        opens = self._count_binary_opens(monkeypatch, paths)

        results = scan_directory(
            [str(tmp_path)], by_hash=True, by_byte=True,
            turbo_mode=turbo_mode, use_cache=False,
        )

        (group,) = results.values()
        assert {f.path for f in group} == set(paths)
        assert len(opens) == expected_opens

    @pytest.mark.parametrize("turbo_mode", [True, False])
    def test_hash_and_byte_groups_equal_byte_only(self, tmp_path, turbo_mode):
        """M6 equivalence: skipping the byte pass for hash-proven groups
        must yield exactly the groups a literal byte-only scan produces on
        the same tree (same size everywhere: 3xA, 2xB, 1xC alone)."""
        for i in range(3):
            (tmp_path / f"a{i}.bin").write_bytes(b"A" * 5000)
        for i in range(2):
            (tmp_path / f"b{i}.bin").write_bytes(b"B" * 5000)
        (tmp_path / "c.bin").write_bytes(b"C" * 5000)

        hash_and_byte = scan_directory(
            [str(tmp_path)], by_hash=True, by_byte=True,
            turbo_mode=turbo_mode, use_cache=False,
        )
        byte_only = scan_directory([str(tmp_path)], by_hash=False, by_byte=True, use_cache=False)

        def memberships(results):
            return sorted(sorted(f.path for f in files) for files in results.values() if len(files) > 1)

        assert memberships(hash_and_byte) == memberships(byte_only)
        assert memberships(byte_only) == [
            sorted(str(tmp_path / f"a{i}.bin") for i in range(3)),
            sorted(str(tmp_path / f"b{i}.bin") for i in range(2)),
        ]

    def test_byte_only_splits_same_size_late_difference(self, tmp_path):
        """M6 correctness: identical size with the difference deep inside
        the file (past the first chunk boundary) must still split — the
        cached-reference comparison must walk every chunk before declaring
        a match."""
        shared_head = b"S" * 100000
        for name, tail in (("a1", b"A" * 50000), ("a2", b"A" * 50000),
                           ("b1", b"B" * 50000), ("b2", b"B" * 50000)):
            (tmp_path / f"{name}.bin").write_bytes(shared_head + tail)

        results = scan_directory([str(tmp_path)], by_hash=False, by_byte=True, use_cache=False)

        pairs = sorted(
            tuple(sorted(os.path.basename(f.path) for f in files))
            for files in results.values()
        )
        assert pairs == [("a1.bin", "a2.bin"), ("b1.bin", "b2.bin")]

    def test_byte_only_cancel_mid_group_returns_empty(self, tmp_path, monkeypatch):
        """Stage-1 review follow-up: cancel must be honored INSIDE the
        subgroup loop — the old code only checked between groups, so one
        huge group kept comparing after the user pressed Cancel."""
        for name, content in (("a", b"C" * 1000), ("b", b"C" * 1000),
                              ("c", b"C" * 1000), ("d", b"D" * 1000),
                              ("e", b"D" * 1000), ("f", b"D" * 1000)):
            (tmp_path / f"{name}.bin").write_bytes(content)

        cancel_flag = [False]
        real_cmp = scanner.content_matches_bytes

        def cancelling_cmp(expected, path, buffer_size=65536):
            cancel_flag[0] = True  # the user pressed Cancel mid-group
            return real_cmp(expected, path, buffer_size)

        monkeypatch.setattr(scanner, "content_matches_bytes", cancelling_cmp)

        results = scan_directory(
            [str(tmp_path)], by_hash=False, by_byte=True,
            use_cache=False, cancel_flag=cancel_flag,
        )
        assert results == {}


class TestContentMatchesBytesEdges:
    """Stage-1 review follow-up: the boundary contract of the RAM-compare
    helper (M6) — pinned before the byte-phase changes touch it."""

    def test_empty_reference_matches_empty_file(self, tmp_path):
        p = tmp_path / "empty.bin"
        p.write_bytes(b"")
        assert content_matches_bytes(b"", str(p)) is True

    def test_empty_reference_rejects_nonempty_file(self, tmp_path):
        p = tmp_path / "x.bin"
        p.write_bytes(b"x")
        assert content_matches_bytes(b"", str(p)) is False

    def test_content_exactly_one_buffer(self, tmp_path):
        """expected == one full chunk: the "file must end exactly where the
        reference ends" check runs right after the chunk loop."""
        chunk = bytes(range(256)) * 256  # 65536 = default buffer_size
        p = tmp_path / "chunk.bin"
        p.write_bytes(chunk)
        assert content_matches_bytes(chunk, str(p)) is True

    def test_difference_in_last_byte(self, tmp_path):
        content = b"A" * 70000
        p = tmp_path / "a.bin"
        p.write_bytes(content)
        expected = b"A" * 69999 + b"B"
        assert content_matches_bytes(expected, str(p)) is False

    def test_identical_multichunk_content(self, tmp_path):
        payload = os.urandom(200_000)  # several buffer sizes
        p = tmp_path / "r.bin"
        p.write_bytes(payload)
        assert content_matches_bytes(payload, str(p)) is True

    def test_candidate_shorter_than_reference(self, tmp_path):
        p = tmp_path / "short.bin"
        p.write_bytes(b"A" * 100)
        assert content_matches_bytes(b"A" * 101, str(p)) is False

    def test_candidate_longer_than_reference(self, tmp_path):
        p = tmp_path / "long.bin"
        p.write_bytes(b"A" * 101)
        assert content_matches_bytes(b"A" * 100, str(p)) is False

    def test_missing_file_returns_false(self, tmp_path):
        assert content_matches_bytes(b"A", str(tmp_path / "nope.bin")) is False


class TestLoadByteReference:
    """Stage-1 review follow-up: the reference snapshot must match the
    INDEXED size — a file that changed on disk since indexing must not
    anchor a subgroup with stale/shifted content (fall back to streaming)."""

    def test_returns_content_when_size_matches(self, tmp_path):
        p = tmp_path / "ref.bin"
        p.write_bytes(b"reference")
        assert load_byte_reference(str(p), 9) == b"reference"

    def test_shrunk_file_returns_none(self, tmp_path):
        p = tmp_path / "ref.bin"
        p.write_bytes(b"123456789")  # 9 bytes on disk
        assert load_byte_reference(str(p), 10) is None  # indexed as 10

    def test_grown_file_returns_none(self, tmp_path):
        p = tmp_path / "ref.bin"
        p.write_bytes(b"1234567890")  # 10 bytes on disk
        assert load_byte_reference(str(p), 9) is None  # indexed as 9

    def test_unreadable_file_returns_none(self, tmp_path):
        assert load_byte_reference(str(tmp_path / "nope.bin"), 5) is None

    def test_oversized_reference_returns_none(self, tmp_path):
        """Over the RAM cap: stream instead, regardless of disk content."""
        p = tmp_path / "big.bin"
        p.write_bytes(b"x" * 10)
        assert load_byte_reference(str(p), BYTE_REFERENCE_CACHE_LIMIT + 1) is None


class TestPhysicalFileDedup:
    """Round 3: the same PHYSICAL file reachable by two paths (hardlink,
    junction alias, overlapping scan roots) must never be reported as a
    duplicate of itself — deleting the "duplicate" would destroy the
    "original" it points to."""

    def test_hardlink_alias_not_grouped_as_duplicate(self, tmp_path):
        original = tmp_path / "original.bin"
        original.write_bytes(b"SAME CONTENT" * 50)
        alias = tmp_path / "alias.bin"
        try:
            os.link(str(original), str(alias))  # real NTFS hardlink
        except (OSError, AttributeError):
            pytest.skip("hardlinks unsupported on this filesystem")

        results = scan_directory([str(tmp_path)], by_hash=True, use_cache=False)

        # One physical file → no duplicate group, even though two paths match.
        assert results == {}

    def test_recycle_bin_copy_not_indexed(self, tmp_path):
        # A live file and its older copy in $Recycle.Bin: the bin copy must
        # never become the "original" that marks the live file deletable.
        live = tmp_path / "live.bin"
        live.write_bytes(b"IMPORTANT" * 50)
        bin_dir = tmp_path / "$Recycle.Bin" / "S-1-5-21"
        bin_dir.mkdir(parents=True)
        (bin_dir / "$R1A2B3C.bin").write_bytes(b"IMPORTANT" * 50)

        results = scan_directory([str(tmp_path)], by_hash=True, use_cache=False)

        # The bin copy is pruned, so the live file has no duplicate to pair with.
        assert results == {}

    def test_system_volume_information_pruned(self, tmp_path):
        live = tmp_path / "data.bin"
        live.write_bytes(b"X" * 200)
        svi = tmp_path / "System Volume Information"
        svi.mkdir()
        (svi / "shadow.bin").write_bytes(b"X" * 200)

        results = scan_directory([str(tmp_path)], by_hash=True, use_cache=False)
        assert results == {}

    def test_junction_to_external_dir_not_followed(self, tmp_path):
        """A junction pointing OUTSIDE the scan root must not pull external
        files into the results (and must not risk an infinite loop)."""
        import subprocess
        external = tmp_path / "external"
        external.mkdir()
        (external / "ext.bin").write_bytes(b"JUNCTION TARGET" * 20)
        root = tmp_path / "root"
        root.mkdir()
        (root / "live.bin").write_bytes(b"JUNCTION TARGET" * 20)  # would pair via junction
        link = root / "link"
        try:
            subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(link), str(external)],
                check=True, capture_output=True, timeout=15,
            )
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            pytest.skip("cannot create junction on this system")

        results = scan_directory([str(root)], by_hash=True, use_cache=False)

        # Junction pruned → only live.bin indexed → no duplicate group.
        assert results == {}


class TestScanForSample:
    def _sample_tree(self, tmp_path):
        src = tmp_path / "src"
        dst = tmp_path / "dst"
        src.mkdir()
        dst.mkdir()
        (src / "sample.bin").write_bytes(b"SAMPLE" * 100)
        (dst / "copy.bin").write_bytes(b"SAMPLE" * 100)   # same content, other name
        (dst / "sample.bin").write_bytes(b"SAMPLE" * 100)  # same content, same name
        (dst / "other.bin").write_bytes(b"OTHER" * 100)
        return src / "sample.bin", dst

    def test_finds_all_content_copies(self, tmp_path):
        sample, dst = self._sample_tree(tmp_path)
        found = scan_for_sample(str(sample), [str(dst)])
        paths = {f.path for f in found}
        assert str(dst / "copy.bin") in paths
        assert str(dst / "sample.bin") in paths
        assert str(dst / "other.bin") not in paths

    def test_sample_file_itself_excluded(self, tmp_path):
        sample, dst = self._sample_tree(tmp_path)
        found = scan_for_sample(str(sample), [str(tmp_path)])
        assert str(sample) not in {f.path for f in found}

    def test_sample_hardlink_alias_excluded(self, tmp_path):
        """Round 3: a hardlink/junction alias of the SAMPLE is the same
        physical file — reporting it as a "copy" would let the user delete
        the sample itself under another path."""
        sample, dst = self._sample_tree(tmp_path)
        alias = tmp_path / "sample_alias.bin"
        try:
            os.link(str(sample), str(alias))  # alias of the sample itself
        except (OSError, AttributeError):
            pytest.skip("hardlinks unsupported on this filesystem")

        found = scan_for_sample(str(sample), [str(tmp_path)])
        paths = {f.path for f in found}

        assert str(sample) not in paths
        assert str(alias) not in paths  # the sample's own alias is excluded
        # The genuine content copies are still found.
        assert str(dst / "copy.bin") in paths

    def test_by_name_restricts_matches(self, tmp_path):
        sample, dst = self._sample_tree(tmp_path)
        found = scan_for_sample(str(sample), [str(dst)], by_name=True)
        assert {f.path for f in found} == {str(dst / "sample.bin")}

    def test_missing_sample_returns_empty(self, tmp_path):
        assert scan_for_sample(str(tmp_path / "nope.bin"), [str(tmp_path)]) == []

    def test_changed_copy_not_reported(self, tmp_path):
        sample, dst = self._sample_tree(tmp_path)
        (dst / "sample.bin").write_bytes(b"SAMPLE" * 99 + b"X")
        found = scan_for_sample(str(sample), [str(dst)])
        assert str(dst / "sample.bin") not in {f.path for f in found}

    def test_size_prefilter_skips_hashing_m5(self, tmp_path, monkeypatch):
        """Regression for M5: wrong-size files must never be hashed at all —
        the old code single-threadedly hashed every candidate full-length."""
        sample, dst = self._sample_tree(tmp_path)
        for i in range(20):
            (dst / f"wrong{i}.bin").write_bytes(b"OTHER" * (i + 1))
        calls = []
        real_hash = scanner.get_file_hash

        def counting_hash(path, block_size=65536):
            calls.append(path)
            return real_hash(path, block_size)

        monkeypatch.setattr(scanner, "get_file_hash", counting_hash)
        found = scan_for_sample(str(sample), [str(dst)])
        assert {f.path for f in found} == {str(dst / "copy.bin"), str(dst / "sample.bin")}
        # Sample itself + the two size-matching candidates — nothing else.
        assert len(calls) == 3


class TestCompareFolders:
    def _tree(self, tmp_path):
        a = tmp_path / "folderA"
        b = tmp_path / "folderB"
        a.mkdir()
        b.mkdir()
        (a / "only_a.txt").write_text("A")
        (b / "only_b.txt").write_text("B")
        (a / "same.bin").write_bytes(b"S" * 100)
        (b / "same.bin").write_bytes(b"S" * 100)
        # Pin identical mtimes: two back-to-back writes can straddle an NTFS
        # clock tick, which would flake "newer" between runs.
        now = time.time()
        os.utime(str(a / "same.bin"), (now, now))
        os.utime(str(b / "same.bin"), (now, now))
        return a, b

    def test_result_schema(self, tmp_path):
        a, b = self._tree(tmp_path)
        res = compare_folders(str(a), str(b))
        assert set(res.keys()) == {"unique_a", "unique_b", "common", "total_files"}

    def test_unique_files_classified(self, tmp_path):
        a, b = self._tree(tmp_path)
        res = compare_folders(str(a), str(b))
        assert {f.name for f in res["unique_a"]} == {"only_a.txt"}
        assert {f.name for f in res["unique_b"]} == {"only_b.txt"}

    def test_common_entry_schema(self, tmp_path):
        a, b = self._tree(tmp_path)
        res = compare_folders(str(a), str(b))
        (entry,) = res["common"]
        assert set(entry.keys()) >= {"file_a", "file_b", "similarity", "newer", "larger"}
        assert isinstance(entry["file_a"], FileInfo)
        assert isinstance(entry["file_b"], FileInfo)

    def test_identical_common_file(self, tmp_path):
        a, b = self._tree(tmp_path)
        (entry,) = compare_folders(str(a), str(b))["common"]
        assert entry["similarity"] == 1.0
        assert entry["newer"] == "same"
        assert entry["larger"] == "same"

    def test_newer_and_larger_detection(self, tmp_path):
        a, b = self._tree(tmp_path)
        (a / "same.bin").write_bytes(b"S" * 150)
        future = time.time() + 100
        os.utime(str(a / "same.bin"), (future, future))
        (entry,) = compare_folders(str(a), str(b))["common"]
        assert entry["newer"] == "a"
        assert entry["larger"] == "a"

    def test_total_files(self, tmp_path):
        a, b = self._tree(tmp_path)
        res = compare_folders(str(a), str(b))
        assert res["total_files"] == 3  # only_a, only_b, same


class TestCalculateSimilarity:
    def test_identical_text_files(self, tmp_path):
        a, b = tmp_path / "a.txt", tmp_path / "b.txt"
        a.write_text("line1\nline2\nline3\n", encoding="utf-8")
        b.write_text("line1\nline2\nline3\n", encoding="utf-8")
        assert calculate_similarity(str(a), str(b)) == 1.0

    def test_different_text_files(self, tmp_path):
        a, b = tmp_path / "a.txt", tmp_path / "b.txt"
        a.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
        b.write_text("alpha\nbeta\ndelta\nepsilon\n", encoding="utf-8")
        assert 0.0 < calculate_similarity(str(a), str(b)) < 1.0

    def test_both_empty_files(self, tmp_path):
        a, b = tmp_path / "a.txt", tmp_path / "b.txt"
        a.write_text("")
        b.write_text("")
        assert calculate_similarity(str(a), str(b)) == 1.0

    def test_identical_binary_files(self, tmp_path):
        a, b = tmp_path / "a.bin", tmp_path / "b.bin"
        a.write_bytes(os.urandom(32))
        b.write_bytes(a.read_bytes())
        assert calculate_similarity(str(a), str(b)) == 1.0


class TestComputeWastedBytes:
    """M9: freed space is the sum of the ACTUAL duplicate sizes, not
    (n - 1) * size(first file) — groups can hold differently-sized members."""

    @staticmethod
    def _info(path, size):
        return FileInfo(path, os.path.basename(path), size, 0.0, 0.0)

    def test_equal_sized_group(self):
        files = [self._info("a.bin", 100), self._info("b.bin", 100), self._info("c.bin", 100)]
        assert scanner.compute_wasted_bytes({"h": files}) == 200

    def test_unequal_sizes_sum_actual_dupes(self):
        files = [self._info("a.bin", 50), self._info("b.bin", 80), self._info("c.bin", 70)]
        # Old formula lied: (3 - 1) * 50 = 100; the real reclaimable space is
        # the size of every file except one, i.e. 80 + 70.
        assert scanner.compute_wasted_bytes({"h": files}) == 150

    def test_empty_and_single_file_group(self):
        assert scanner.compute_wasted_bytes({}) == 0
        assert scanner.compute_wasted_bytes({"h": [self._info("a.bin", 10)]}) == 0

    def test_multiple_groups_summed(self):
        g1 = [self._info("a.bin", 10), self._info("b.bin", 10)]
        g2 = [self._info("c.bin", 5), self._info("d.bin", 9)]
        assert scanner.compute_wasted_bytes({"h1": g1, "h2": g2}) == 19


class TestCriteriaGuard:
    """Round-2 P0: with every criterion disabled the scan used to return ALL
    files as one duplicate group, pre-selected for deletion in the UI."""

    def test_scan_directory_rejects_all_criteria_off(self, tmp_path):
        with pytest.raises(ValueError):
            scan_directory([str(tmp_path)], by_name=False, by_size=False, by_hash=False, by_byte=False)

    def test_scan_for_sample_rejects_all_criteria_off(self, tmp_path):
        sample = tmp_path / "s.txt"
        sample.write_text("x")
        with pytest.raises(ValueError):
            scan_for_sample(str(sample), [str(tmp_path)], by_name=False, by_size=False, by_hash=False, by_byte=False)


class TestCancelDuringHash:
    """Round-2 P0: a cancelled scan must return immediately. The old
    `with ThreadPoolExecutor` block joined in-flight tasks on exit
    (shutdown(wait=True) via __exit__), so "cancelled" scans kept hashing."""

    def test_cancel_returns_without_waiting_for_running_hashes(self, tmp_path, monkeypatch):
        """Deterministic (no wall-clock threshold, no startup race).

        7 straggler tasks block on an Event and signal once they are inside
        hashing; f0 waits for that signal, THEN flips the cancel flag. So at
        the moment the scan can return, all 7 are provably in flight. If the
        scan returned without joining them (the fix), they are still blocked;
        the old `with ThreadPoolExecutor` block joined them, draining the list.
        """
        for i in range(8):
            (tmp_path / f"f{i}.bin").write_bytes(b"x" * 100)

        n_stragglers = 7
        flag = [False]
        in_flight = []
        started = []
        all_started = threading.Event()
        release = threading.Event()
        lock = threading.Lock()
        real_hash = scanner.get_file_hash

        def blocking_hash(path):
            if os.path.basename(path) == "f0.bin":
                # Cancel only once every straggler is confirmed in flight.
                all_started.wait(timeout=5)
                flag[0] = True
                return real_hash(path)
            in_flight.append(path)
            with lock:
                started.append(path)
                if len(started) == n_stragglers:
                    all_started.set()
            try:
                release.wait(timeout=5)
            finally:
                in_flight.remove(path)
            return real_hash(path)

        monkeypatch.setattr(scanner, "get_file_hash", blocking_hash)

        results = scan_directory(
            [str(tmp_path)],
            by_hash=True,
            turbo_mode=False,
            use_cache=False,
            cancel_flag=flag,
            max_workers=8,
        )
        still_running = list(in_flight)  # snapshot BEFORE releasing stragglers
        release.set()

        assert results == {}
        assert len(still_running) == n_stragglers, (
            "scan joined the running hashes instead of returning immediately"
        )

    def test_queued_hash_tasks_skip_work_after_cancel(self, tmp_path, monkeypatch):
        """The per-task cancel check: with 1 worker and 6 files, flipping the
        flag during the first hash must prevent the rest from hashing at all."""
        for i in range(6):
            (tmp_path / f"g{i}.bin").write_bytes(b"x" * 100)

        flag = [False]
        calls = {"n": 0}

        def counting_hash(path):
            calls["n"] += 1
            flag[0] = True  # cancel everything after the very first hash
            return "deadbeef"

        monkeypatch.setattr(scanner, "get_file_hash", counting_hash)

        results = scan_directory(
            [str(tmp_path)],
            by_hash=True,
            turbo_mode=False,
            use_cache=False,
            cancel_flag=flag,
            max_workers=1,
        )
        assert results == {}
        assert calls["n"] == 1


class TestExcludePatternMatching:
    """Round 2: exclude patterns must match WHOLE path components — the old
    substring matching let a "Temp" chip swallow D:\\Templates and
    "attempt_final", silently shrinking scan results."""

    def test_pattern_matches_whole_components_only(self, tmp_path):
        # NB: names deliberately unlike any ancestor of pytest's tmp_path
        # (which lives under ...\Temp\... and a "Temp" chip would match it too).
        for d in ("no", "notebook", "note_final"):
            (tmp_path / d).mkdir()
            (tmp_path / d / "dup.bin").write_bytes(b"Z" * 512)

        results = scan_directory(
            [str(tmp_path)], by_size=True, by_hash=True,
            exclude_patterns=["No"], use_cache=False,
        )
        # Old behavior: substring "*no*" excluded all three dirs -> no group.
        assert len(results) == 1
        group = next(iter(results.values()))
        parent_names = {os.path.basename(os.path.dirname(f.path)) for f in group}
        assert parent_names == {"notebook", "note_final"}

    def test_wildcard_pattern_matches_component_names(self, tmp_path):
        (tmp_path / "keep.txt").write_bytes(b"A" * 100)
        (tmp_path / "app.logger.txt").write_bytes(b"A" * 100)  # '*.log' must NOT match this
        (tmp_path / "app.log").write_bytes(b"A" * 100)
        (tmp_path / "logs").mkdir()
        (tmp_path / "logs" / "x.log").write_bytes(b"A" * 100)

        results = scan_directory(
            [str(tmp_path)], by_size=True, by_hash=True,
            exclude_patterns=["*.log"], use_cache=False,
        )
        paths = {f.path for files in results.values() for f in files}
        assert paths == {str(tmp_path / "keep.txt"), str(tmp_path / "app.logger.txt")}

    def test_path_pattern_with_separator_excludes_subtree(self, tmp_path):
        """Round 3: a pattern containing a separator (an absolute/relative path
        like 'D:\\Games') must exclude that subtree. The component-only matcher
        silently ignored such patterns, excluding nothing."""
        keep = tmp_path / "keep"
        skip = tmp_path / "skip"
        keep.mkdir()
        skip.mkdir()
        (keep / "f.bin").write_bytes(b"DUP" * 100)
        (skip / "f.bin").write_bytes(b"DUP" * 100)

        results = scan_directory(
            [str(tmp_path)], by_hash=True, use_cache=False,
            exclude_patterns=[str(skip)],
        )
        # Only keep/f.bin survives → no duplicate pair → empty result.
        # (Old code excluded nothing → the two files grouped → non-empty.)
        assert results == {}


class TestCompareCaseInsensitive:
    """Round 2: Windows paths are case-insensitive — without normcase keys,
    Report.PDF vs report.pdf read as two different files, each "unique"."""

    def test_case_only_difference_is_common(self, tmp_path):
        a = tmp_path / "a"
        b = tmp_path / "b"
        a.mkdir()
        b.mkdir()
        (a / "Report.PDF").write_bytes(b"same data")
        (b / "report.pdf").write_bytes(b"same data")

        result = compare_folders(str(a), str(b))

        assert result["unique_a"] == []
        assert result["unique_b"] == []
        assert len(result["common"]) == 1
        assert result["common"][0]["similarity"] == 1.0


class TestCandidateScaleReport:
    def test_candidate_groups_reported_before_hashing(self, dup_tree):
        messages = []
        scan_directory(
            [str(dup_tree)], by_size=True, by_hash=True,
            use_cache=False, progress_callback=lambda m, p: messages.append(m),
        )
        assert any("candidate" in m.lower() for m in messages)


class TestUnicodePaths:
    """RU users have Cyrillic/spaced paths everywhere — the whole pipeline
    (walk, stat, hash, group) must be agnostic to that."""

    def test_cyrillic_and_spaced_names_grouped(self, tmp_path):
        d1 = tmp_path / "папка №5"
        d2 = tmp_path / "Отчёт копия (2)"
        d1.mkdir()
        d2.mkdir()
        (d1 / "Данные файл.bin").write_bytes(b"U" * 100)
        (d2 / "Данные файл.bin").write_bytes(b"U" * 100)

        results = scan_directory([str(tmp_path)], by_size=True, by_hash=True, use_cache=False)

        assert len(results) == 1
        assert len(next(iter(results.values()))) == 2
