"""Tests for scanner.py — hashing, grouping, filters, turbo verification."""
import hashlib
import os
import time

import pytest

import scanner
from scanner import (
    FileInfo,
    calculate_similarity,
    compare_byte_by_byte,
    compare_folders,
    format_file_size,
    get_file_category,
    get_file_hash,
    get_turbo_hash,
    is_system_path,
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
        content = b"duplicater test content" * 100
        p.write_bytes(content)
        expected = hashlib.sha256(content).hexdigest()
        assert get_file_hash(str(p)) == expected

    def test_get_file_hash_missing_file_returns_none(self, tmp_path):
        assert get_file_hash(str(tmp_path / "missing.bin")) is None

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

    def test_cache_roundtrip_second_scan_identical(self, dup_tree):
        first = scan_directory([str(dup_tree)])
        # Second scan must hit the SQLite cache and still group correctly.
        second = scan_directory([str(dup_tree)])
        assert {k: [f.path for f in v] for k, v in first.items()} == \
               {k: [f.path for f in v] for k, v in second.items()}

    def test_cache_invalidated_after_file_change(self, dup_tree):
        scan_directory([str(dup_tree)])
        # Rewrite two of the three duplicates with different content
        # → no same-content pair remains → group must dissolve.
        (dup_tree / "dir2" / "dup.bin").write_bytes(b"Z" * 1024)
        (dup_tree / "dir3" / "dup.bin").write_bytes(b"Y" * 1024)
        results = scan_directory([str(dup_tree)])
        assert results == {}

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

    def_turbo = None

    def test_byte_by_byte_verification(self, tmp_path):
        block = 65536
        a = tmp_path / "a.bin"
        b = tmp_path / "b.bin"
        a.write_bytes(b"A" * block + b"MID-A" + b"A" * block)
        b.write_bytes(b"A" * block + b"MID-B" + b"A" * block)
        results = scan_directory([str(tmp_path)], by_byte=True)
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
