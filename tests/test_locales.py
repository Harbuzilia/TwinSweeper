"""Tests for locales.py — RU/EN key parity and placeholder consistency."""
import re

import pytest

import locales
from locales import get_text, translations


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

    def test_at_least_100_keys(self, en_keys):
        assert len(en_keys) >= 100


class TestPlaceholderConsistency:
    """A mismatched {} count between RU and EN crashes .format() at runtime."""

    def test_format_placeholder_counts_match(self, en_keys):
        problems = []
        for key in sorted(en_keys):
            en_val = translations["en"].get(key, "")
            ru_val = translations["ru"].get(key, "")
            n_en = en_val.count("{}")
            n_ru = ru_val.count("{}")
            if n_en != n_ru:
                problems.append(f"{key}: en has {n_en}, ru has {n_ru}")
        assert not problems, "\n".join(problems)

    def test_all_translations_format_cleanly(self):
        filler = "X"
        for lang, table in translations.items():
            for key, value in table.items():
                if "{}" in value:
                    # .format() must not raise and must consume every placeholder
                    formatted = value.format(*(filler,) * value.count("{}"))
                    assert filler in formatted


class TestGetText:
    def test_default_language_is_russian(self):
        assert get_text("app_title") == translations["ru"]["app_title"]

    def test_explicit_language(self):
        assert get_text("app_title", "en") == translations["en"]["app_title"]

    def test_unknown_language_falls_back_to_ru(self):
        assert get_text("app_title", "fr") == translations["ru"]["app_title"]

    def test_unknown_key_returns_key_itself(self):
        assert get_text("totally_unknown_key_xyz", "en") == "totally_unknown_key_xyz"

    def test_unknown_key_in_unknown_language(self):
        assert get_text("totally_unknown_key_xyz", "fr") == "totally_unknown_key_xyz"
