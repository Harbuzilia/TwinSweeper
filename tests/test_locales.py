"""Tests for locales.py — RU/EN key parity and placeholder consistency."""
import re

import pytest

import locales
from locales import get_text, set_current_language, translations


@pytest.fixture(scope="module")
def en_keys():
    return set(translations["en"].keys())


@pytest.fixture(scope="module")
def ru_keys():
    return set(translations["ru"].keys())


class TestKeyParity:
    def test_every_en_key_has_ru_translation(self, en_keys, ru_keys):
        missing = en_keys - ru_keys
        assert not missing, f"Keys missing from RU: {sorted(missing)}"

    def test_every_ru_key_has_en_translation(self, en_keys, ru_keys):
        missing = ru_keys - en_keys
        assert not missing, f"Keys missing from EN: {sorted(missing)}"

    def test_no_empty_translations(self):
        for lang, table in translations.items():
            empty = [k for k, v in table.items() if not isinstance(v, str) or not v.strip()]
            assert not empty, f"Empty values in {lang}: {empty}"

    def test_translation_table_is_substantial(self, en_keys, ru_keys):
        # Guards against accidental truncation of the table. (The old
        # ">= 100" floor was unreachable-low with 400+ keys and proved
        # nothing; specific keys are pinned by test_round2_keys_exist_paired.)
        assert len(en_keys) >= 250
        assert len(ru_keys) >= 250


def _placeholder_arity(value: str) -> int:
    """Number of positional args a format string needs, counting BOTH {} and
    {0}-style fields (the table mixes them)."""
    import string
    fields = [fname for _, fname, _, _ in string.Formatter().parse(value) if fname is not None]
    numbered = [int(f) for f in fields if f.isdigit()]
    return max(len(fields), (max(numbered) + 1) if numbered else 0)


class TestPlaceholderConsistency:
    """A mismatched placeholder arity between RU and EN crashes .format() at
    runtime — and the old check only counted `{}`, missing every `{0}` key."""

    def test_format_placeholder_arity_matches(self, en_keys):
        problems = []
        for key in sorted(en_keys):
            en_val = translations["en"].get(key, "")
            ru_val = translations["ru"].get(key, "")
            if _placeholder_arity(en_val) != _placeholder_arity(ru_val):
                problems.append(
                    f"{key}: en needs {_placeholder_arity(en_val)}, ru needs {_placeholder_arity(ru_val)}"
                )
        assert not problems, "\n".join(problems)

    def test_all_translations_format_cleanly(self):
        # Real check: format every value with exactly its own arity. Mixing
        # {} and {0} in one string, or a stray brace, raises here.
        problems = []
        for lang, table in translations.items():
            for key, value in table.items():
                arity = _placeholder_arity(value)
                try:
                    value.format(*(["X"] * arity))
                except (IndexError, KeyError, ValueError) as ex:
                    problems.append(f"{lang}:{key}: {ex}")
        assert not problems, "\n".join(problems)


class TestGetText:
    def test_no_arg_get_text_uses_current_language(self):
        # Round 2: the no-arg default follows set_current_language (English
        # until the app says otherwise) — core modules rely on this for
        # progress/error strings.
        assert get_text("app_title") == translations["en"]["app_title"]

    def test_explicit_language(self):
        assert get_text("app_title", "en") == translations["en"]["app_title"]

    def test_unknown_language_falls_back_to_ru(self):
        assert get_text("app_title", "fr") == translations["ru"]["app_title"]

    def test_unknown_key_returns_key_itself(self):
        assert get_text("totally_unknown_key_xyz", "en") == "totally_unknown_key_xyz"

    def test_unknown_key_in_unknown_language(self):
        assert get_text("totally_unknown_key_xyz", "fr") == "totally_unknown_key_xyz"


class TestCoreModuleLanguage:
    """Round 2: core modules (scanner/sweeper/phash/hardlink) call get_text()
    without an explicit language; main.py switches it together with the UI
    language. The default stays English so test expectations are stable."""

    def test_default_is_english(self):
        assert get_text("scan_cancelled") == "Scan was cancelled."

    def test_set_current_language_switches_core_strings(self):
        try:
            set_current_language("ru")
            assert get_text("scan_cancelled") == "Сканирование отменено пользователем."
        finally:
            set_current_language("en")

    def test_explicit_lang_beats_current_language(self):
        try:
            set_current_language("ru")
            assert get_text("scan_cancelled", "en") == "Scan was cancelled."
        finally:
            set_current_language("en")

    def test_unknown_language_is_ignored(self):
        set_current_language("klingon")
        assert get_text("scan_cancelled") == "Scan was cancelled."

    def test_round2_keys_exist_paired(self, en_keys, ru_keys):
        expected = {
            "scan_error", "scan_phase_indexing", "scan_indexed", "scan_phase_analyzing",
            "scan_hashed", "scan_verifying_full", "scan_phase_byte", "scan_byte_progress",
            "scan_complete", "sample_scanned", "compare_progress",
            "sweep_checked_dirs", "sweep_analyzed_shortcuts", "sweep_scanned_junk",
            "sweep_remove_failed",
            "phash_discovering", "phash_hashing", "phash_hashed", "phash_clustering",
            "phash_skipped_unsupported",
            "verify_failed_access", "verify_failed_size", "verify_failed_mtime",
            "dlg_errors_list", "dlg_warnings_list",
            "hl_err_original_not_found", "hl_err_duplicate_not_found", "hl_err_same_file",
            "hl_err_cross_volume", "hl_err_size_diff", "hl_err_content_diff",
            "hl_err_stale_tmp", "hl_err_win32", "hl_warn_backup_leftover",
            "hl_err_original_missing", "hl_msg_linked_with_warning", "hl_msg_failed",
            "compare_no_common", "compare_no_unique_a", "compare_no_unique_b",
            "compare_files_count", "collapse_tooltip", "unknown_size", "more_items_suffix",
        }
        assert expected <= en_keys
        assert expected <= ru_keys
