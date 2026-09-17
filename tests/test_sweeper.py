"""Tests for sweeper.py — empty folders, junk files, broken .lnk shortcuts."""
import os

import pytest

from sweeper import (
    delete_empty_directories,
    find_broken_shortcuts,
    find_empty_directories,
    find_junk_files,
    resolve_windows_shortcut_target,
)


def make_lnk(path, target, local_base_path_offset=0x1C, include_link_info=True, encoding="utf-8"):
    """Hand-crafted minimal ShellLink (.lnk) binary per MS-SHLLINK layout.

    Header (0x4C bytes) + LinkInfo block with a local base path string.
    The parser reads LocalBasePathOffset at LinkInfo+0x10.
    """
    header = bytearray(0x4C)
    header[0:4] = (0x4C).to_bytes(4, "little")  # HeaderSize
    header[4:20] = bytes.fromhex("0114020000000000C000000000000046")  # LinkCLSID
    flags = 0x02 if include_link_info else 0x00  # HasLinkInfo
    header[0x14:0x18] = flags.to_bytes(4, "little")

    out = bytearray(header)
    if include_link_info:
        target_bytes = target.encode(encoding)
        link_info = bytearray(0x1C)  # header fields only; path goes right after
        link_info[0x10:0x14] = local_base_path_offset.to_bytes(4, "little")
        link_info[0x08:0x0C] = (1).to_bytes(4, "little")  # VolumeIDAndLocalBasePath
        out += link_info + target_bytes + b"\x00"
        out[0x4C:0x50] = (len(out) - 0x4C).to_bytes(4, "little")  # LinkInfoSize

    with open(path, "wb") as f:
        f.write(bytes(out))
    return str(path)


class TestEmptyDirectories:
    def test_nested_empty_tree_found(self, tmp_path):
        root = tmp_path / "root"
        (root / "a" / "b" / "c").mkdir(parents=True)
        (root / "file.txt").write_text("x")
        found = find_empty_directories([str(root)])
        assert set(found) == {
            str(root / "a" / "b" / "c"),
            str(root / "a" / "b"),
            str(root / "a"),
        }

    def test_search_root_itself_never_listed(self, tmp_path):
        root = tmp_path / "empty_root"
        root.mkdir()
        assert find_empty_directories([str(root)]) == []

    def test_dirs_with_files_not_listed(self, tmp_path):
        root = tmp_path / "root"
        (root / "full").mkdir(parents=True)
        (root / "full" / "data.txt").write_text("x")
        assert find_empty_directories([str(root)]) == []

    def test_cancel_flag(self, tmp_path):
        root = tmp_path / "root"
        (root / "a" / "b").mkdir(parents=True)
        assert find_empty_directories([str(root)], cancel_flag=[True]) == []

    def test_nonexistent_directory_skipped(self, tmp_path):
        assert find_empty_directories([str(tmp_path / "nope")]) == []

    def test_delete_empty_directories(self, tmp_path):
        root = tmp_path / "root"
        (root / "a" / "b").mkdir(parents=True)
        targets = [str(root / "a" / "b"), str(root / "a")]
        deleted, errors = delete_empty_directories(targets)
        assert deleted == 2
        assert errors == []
        assert not (root / "a").exists()

    def test_delete_non_empty_directory_reports_error(self, tmp_path):
        root = tmp_path / "root"
        root.mkdir()
        (root / "file.txt").write_text("x")
        deleted, errors = delete_empty_directories([str(root)])
        assert deleted == 0
        assert len(errors) == 1
        assert root.exists()


class TestJunkFiles:
    @pytest.mark.parametrize("name", ["temp.tmp", "backup.bak", "old.old", "dump.dmp", "log.log", "gid.gid", "chk.chk"])
    def test_junk_extensions_found(self, tmp_path, name):
        (tmp_path / name).write_text("x")
        junk = find_junk_files([str(tmp_path)])
        assert str(tmp_path / name) in {f.path for f in junk}

    def test_thumbs_db_is_junk(self, tmp_path):
        (tmp_path / "Thumbs.db").write_bytes(b"\x00")
        junk = find_junk_files([str(tmp_path)])
        assert str(tmp_path / "Thumbs.db") in {f.path for f in junk}

    def test_desktop_ini_is_not_junk(self, tmp_path):
        """Regression for H4: desktop.ini is a legitimate Windows file."""
        (tmp_path / "desktop.ini").write_text("[.ShellClassInfo]")
        junk = find_junk_files([str(tmp_path)])
        assert str(tmp_path / "desktop.ini") not in {f.path for f in junk}

    def test_normal_files_not_junk(self, tmp_path):
        (tmp_path / "doc.txt").write_text("x")
        (tmp_path / "photo.jpg").write_bytes(b"\x00" * 10)
        assert find_junk_files([str(tmp_path)]) == []

    def test_recursive_search(self, tmp_path):
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "cache.tmp").write_text("x")
        junk = find_junk_files([str(tmp_path)])
        assert str(tmp_path / "sub" / "cache.tmp") in {f.path for f in junk}

    def test_cancel_flag(self, tmp_path):
        (tmp_path / "a.tmp").write_text("x")
        assert find_junk_files([str(tmp_path)], cancel_flag=[True]) == []

    def test_junk_inside_system_root_skipped(self, tmp_path, monkeypatch):
        fake_root = tmp_path / "FakeWindows"
        fake_root.mkdir()
        monkeypatch.setenv("SystemRoot", str(fake_root))
        (fake_root / "junk.tmp").write_text("x")
        (tmp_path / "user.tmp").write_text("x")
        junk = {f.path for f in find_junk_files([str(tmp_path)])}
        assert str(fake_root / "junk.tmp") not in junk
        assert str(tmp_path / "user.tmp") in junk


class TestShortcuts:
    def test_resolve_valid_local_shortcut(self, tmp_path):
        target = tmp_path / "target.txt"
        target.write_text("x")
        lnk = make_lnk(tmp_path / "valid.lnk", str(target))
        assert resolve_windows_shortcut_target(lnk) == str(target)

    def test_resolve_missing_target(self, tmp_path):
        missing = str(tmp_path / "missing.txt")
        lnk = make_lnk(tmp_path / "broken.lnk", missing)
        assert resolve_windows_shortcut_target(lnk) == missing

    def test_resolve_zero_local_base_path_offset(self, tmp_path):
        """Regression for M10: LocalBasePathOffset == 0 means no local path
        (network-only shortcut) — must resolve to None, not garbage."""
        lnk = make_lnk(tmp_path / "net.lnk", "whatever", local_base_path_offset=0)
        assert resolve_windows_shortcut_target(lnk) is None

    def test_resolve_no_link_info(self, tmp_path):
        lnk = make_lnk(tmp_path / "noinfo.lnk", "ignored", include_link_info=False)
        assert resolve_windows_shortcut_target(lnk) is None

    def test_resolve_not_a_lnk_file(self, tmp_path):
        p = tmp_path / "fake.lnk"
        p.write_bytes(b"\x4c\x00\x00\x00" + b"\x00" * 40)  # truncated header
        assert resolve_windows_shortcut_target(str(p)) is None

    def test_find_broken_shortcuts_reports_only_broken(self, tmp_path):
        target = tmp_path / "good_target.txt"
        target.write_text("x")
        make_lnk(tmp_path / "good.lnk", str(target))
        make_lnk(tmp_path / "broken.lnk", str(tmp_path / "gone.txt"))
        make_lnk(tmp_path / "net.lnk", "x", local_base_path_offset=0)  # M10: not broken
        broken = find_broken_shortcuts([str(tmp_path)])
        assert {b["path"] for b in broken} == {str(tmp_path / "broken.lnk")}
        entry = broken[0]
        assert entry["target"] == str(tmp_path / "gone.txt")
        assert entry["name"] == "broken.lnk"

    def test_find_broken_shortcuts_ignores_other_files(self, tmp_path):
        (tmp_path / "note.txt").write_text("x")
        assert find_broken_shortcuts([str(tmp_path)]) == []

    def test_unicode_target_roundtrip(self, tmp_path):
        target = tmp_path / "Файл Данные.txt"
        target.write_text("x")
        lnk = make_lnk(tmp_path / "юникод.lnk", str(target))
        assert resolve_windows_shortcut_target(lnk) == str(target)

    def test_ansi_encoded_target_decoded_via_mbcs(self, tmp_path):
        """Round 2: real Windows writes LocalBasePath in the SYSTEM ANSI code
        page — the hardcoded cp1251 fallback garbled targets on non-Russian
        Windows and flagged working shortcuts as broken."""
        target = tmp_path / "Цель Анси.txt"
        target.write_text("x")
        lnk = make_lnk(tmp_path / "ansi.lnk", str(target), encoding="mbcs")
        assert resolve_windows_shortcut_target(lnk) == str(target)

    def test_shortcut_to_unavailable_drive_not_broken(self, tmp_path):
        """Round 3: a shortcut whose target is on a disconnected drive / offline
        share is NOT broken — deleting the .lnk would lose a working link. It
        must be skipped, not offered for deletion."""
        import string
        free = next((d for d in string.ascii_uppercase if not os.path.exists(f"{d}:\\")), None)
        if free is None:
            pytest.skip("no free drive letter to simulate an offline target")
        make_lnk(tmp_path / "offline.lnk", f"{free}:\\nonexistent\\target.txt")

        broken = find_broken_shortcuts([str(tmp_path)])

        assert broken == []

    def test_shortcut_to_missing_file_on_live_drive_is_broken(self, tmp_path):
        """Positive control: a target on an AVAILABLE drive that genuinely does
        not exist IS broken and must be reported."""
        target = tmp_path / "definitely_missing.txt"  # never created
        lnk = make_lnk(tmp_path / "broken.lnk", str(target))
        broken = find_broken_shortcuts([str(tmp_path)])
        assert [b["path"] for b in broken] == [lnk]


class TestNestedEmptyDirectoryDeletion:
    """Round-2 P0: callers pass set-derived (hash-random) folder order; deleting
    a parent before its children failed with "not empty" and left the tree
    behind. Deletion must sort deepest-first itself."""

    def test_parent_first_input_still_deletes_whole_chain(self, tmp_path):
        deep = tmp_path / "a" / "b" / "c"
        deep.mkdir(parents=True)
        worst_case_order = [str(tmp_path / "a"), str(tmp_path / "a" / "b"), str(deep)]
        count, errors = delete_empty_directories(worst_case_order)
        assert count == 3
        assert errors == []
        assert not os.path.exists(str(tmp_path / "a"))

    def test_input_duplicates_are_deduped(self, tmp_path):
        d = tmp_path / "solo"
        d.mkdir()
        count, errors = delete_empty_directories([str(d), str(d)])
        assert count == 1
        assert errors == []
        assert not d.exists()

    def test_deep_skeleton_removed_in_one_pass(self, tmp_path):
        root = tmp_path / "skel"
        for i in range(5):
            (root / f"l{i}" / "inner" / "leaf").mkdir(parents=True)
        found = find_empty_directories([str(tmp_path)])
        assert len(found) >= 15
        # Reverse of the (already bottom-up) discovery order — pure adversarial.
        count, errors = delete_empty_directories(list(reversed(found)))
        assert count == len(found)
        assert errors == []
        assert not root.exists()
