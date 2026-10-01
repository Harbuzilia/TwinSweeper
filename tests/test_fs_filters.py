"""Tests for fs_filters.py — shared traversal rules for all three scanners.

Pruning logic is additionally unit-tested on plain name lists (no real
junctions needed), which keeps the coverage independent of dev-mode/admin
privileges (see the RELEASE_PLAN risk table).
"""
import os
import subprocess

import pytest

from fs_filters import (
    InodeDeduper,
    inode_key,
    is_system_path,
    prune_dirs,
)


def make_stat(ino: int, dev: int) -> os.stat_result:
    """A minimal os.stat_result with a controlled (device, inode) pair."""
    return os.stat_result([0o100644, ino, dev, 1, 0, 0, 10, 0.0, 0.0, 0.0])


class TestPruneDirs:
    def test_prunes_system_locations_any_case(self, tmp_path):
        dirs = ["Keep", "$Recycle.Bin", "SYSTEM VOLUME INFORMATION"]
        result = prune_dirs(str(tmp_path), dirs)
        assert result == ["Keep"]
        # In-place: os.walk(topdown=True) only prunes when the SAME list
        # object is mutated.
        assert dirs == ["Keep"]

    def test_similar_names_are_not_pruned(self, tmp_path):
        """Component matching: whole names only, never substrings — a user
        folder named like a system location must survive."""
        dirs = ["$Recycle.Bin.backup", "NotRecycle", "System Volume"]
        prune_dirs(str(tmp_path), dirs)
        assert dirs == ["$Recycle.Bin.backup", "NotRecycle", "System Volume"]

    def test_plain_directories_kept(self, tmp_path):
        dirs = ["photos", "Documents 2024", "папка №5"]
        prune_dirs(str(tmp_path), dirs)
        assert dirs == ["photos", "Documents 2024", "папка №5"]

    def test_junction_pruned_from_dirs(self, tmp_path):
        """followlinks=False refuses symlinks only (os.path.islink);
        Windows junctions pass as plain folders — prune_dirs must cut them."""
        external = tmp_path / "external"
        external.mkdir()
        (external / "ext.bin").write_bytes(b"x")
        link = tmp_path / "link"
        try:
            subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(link), str(external)],
                check=True, capture_output=True, timeout=15,
            )
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            pytest.skip("cannot create junction on this system")

        dirs = ["keep", "link"]
        prune_dirs(str(tmp_path), dirs)
        assert dirs == ["keep"]


class TestInodeKey:
    def test_key_is_dev_ino_tuple(self):
        assert inode_key(make_stat(ino=7, dev=5)) == (5, 7)

    def test_zero_inode_has_no_key(self):
        """FAT/exFAT report st_ino == 0 for every file: without the guard,
        two unrelated files would "dedup" against each other."""
        assert inode_key(make_stat(ino=0, dev=5)) is None


class TestInodeDeduper:
    def test_first_registration_not_seen(self):
        dedup = InodeDeduper()
        assert dedup.already_seen(make_stat(ino=7, dev=5)) is False

    def test_second_registration_seen(self):
        dedup = InodeDeduper()
        dedup.already_seen(make_stat(ino=7, dev=5))
        assert dedup.already_seen(make_stat(ino=7, dev=5)) is True

    def test_zero_inode_never_deduped(self):
        dedup = InodeDeduper()
        assert dedup.already_seen(make_stat(ino=0, dev=5)) is False
        assert dedup.already_seen(make_stat(ino=0, dev=5)) is False

    def test_same_inode_on_different_devices_not_deduped(self):
        """The pair is physical identity; inodes are only unique per device."""
        dedup = InodeDeduper()
        assert dedup.already_seen(make_stat(ino=7, dev=5)) is False
        assert dedup.already_seen(make_stat(ino=7, dev=6)) is False


class TestIsSystemPath:
    """Anchor cases proving the function survived the move from scanner.py
    unchanged — the full matrix lives in tests/test_scanner.py."""

    @pytest.mark.parametrize("path", [
        r"C:\Windows\System32\drivers\etc\hosts",
        r"D:\$RECYCLE.BIN\S-1-5\x",
        r"E:\System Volume Information\x",
    ])
    def test_system_paths_flagged(self, path):
        assert is_system_path(path) is True

    @pytest.mark.parametrize("path", [
        r"E:\AllMyProject\bootcamp\notes.txt",
        r"E:\backup\system32copy\readme.txt",
    ])
    def test_user_paths_with_system_like_substrings_not_flagged(self, path):
        assert is_system_path(path) is False
