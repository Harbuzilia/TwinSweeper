"""Round 2 / Stage 4: priority folders ("keep files from here, clean the rest")."""
import os

import folder_priorities
from folder_priorities import is_priority_path, load_priority_folders, save_priority_folders


class TestPriorityFoldersStore:
    def test_missing_file_returns_empty(self, monkeypatch, tmp_path):
        monkeypatch.setattr(folder_priorities, "PRIORITIES_FILE", str(tmp_path / "priorities.json"))
        assert load_priority_folders() == []

    def test_save_load_roundtrip(self, monkeypatch, tmp_path):
        monkeypatch.setattr(folder_priorities, "PRIORITIES_FILE", str(tmp_path / "priorities.json"))
        folders = [r"D:\Photos", r"E:\Backup\2024"]
        save_priority_folders(folders)
        assert load_priority_folders() == folders

    def test_save_dedupes_and_normalizes(self, monkeypatch, tmp_path):
        monkeypatch.setattr(folder_priorities, "PRIORITIES_FILE", str(tmp_path / "priorities.json"))
        save_priority_folders([r"D:\Photos", r"D:\Photos\sub\..", "  ", r"E:\X"])
        assert load_priority_folders() == [r"D:\Photos", r"E:\X"]

    def test_corrupt_file_returns_empty(self, monkeypatch, tmp_path):
        p = tmp_path / "priorities.json"
        p.write_text("{ nope", encoding="utf-8")
        monkeypatch.setattr(folder_priorities, "PRIORITIES_FILE", str(p))
        assert load_priority_folders() == []


class TestIsPriorityPath:
    PRIOS = [r"D:\Photos", r"E:\Backup"]

    def test_exact_match(self):
        assert is_priority_path(r"D:\Photos", self.PRIOS) is True

    def test_subfolder_match(self):
        assert is_priority_path(r"D:\Photos\2024\img.jpg", self.PRIOS) is True

    def test_case_insensitive_match(self):
        assert is_priority_path(r"d:\pHoToS\IMG.JPG", self.PRIOS) is True

    def test_prefix_component_only(self):
        # "D:\PhotoStudio" is NOT under "D:\Photos" — no substring traps.
        assert is_priority_path(r"D:\PhotoStudio\img.jpg", self.PRIOS) is False

    def test_unrelated_path(self):
        assert is_priority_path(r"C:\Users\x\img.jpg", self.PRIOS) is False

    def test_empty_priorities(self):
        assert is_priority_path(r"D:\Photos\img.jpg", []) is False
