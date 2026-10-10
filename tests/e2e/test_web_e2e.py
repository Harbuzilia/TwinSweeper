"""Пострелизный e2e-гейт web-режима TwinSweeper (Flet 0.86.5).

Регрессионный UI-спек поверх живого web-сервера (фикстура ``app_server``
в conftest.py). Запуск вручную, в дефолтный прогон ``pytest tests/`` НЕ
входит (маркер ``e2e`` отсеивается через addopts в pytest.ini):

    venv\\Scripts\\python -m pytest tests/e2e -m e2e

Механика веб-рендера Flet (проверена QA-сессией rc5, артефакты
``%TEMP%\\opencode\\steps_rc5*.json``): UI живёт в canvas, доступный DOM —
дерево ``flt-semantics``; до клика по ``flt-semantics-placeholder``
семантика отключена. Кнопки ищутся как ``flt-semantics[role=button]`` по
``textContent``, клик — нативным ``el.click()`` из JS (Playwright-клик по
поверх canvas ненадёжен).

Каждый тест получает свежую страницу = свежую Flet-сессию (main() исполняется
заново), поэтому тесты НЕ зависят от порядка и состояния друг друга; сервер
один на модуль.

Ограничение web-режима: нативный folder picker (системный диалог браузера)
автоматизации Playwright не поддаётся и здесь НЕ проверяется — папка
сканирования задаётся программно CLI-аргументом main.py (см. conftest.py).
"""

import json
import time
from pathlib import Path

import pytest

from app_info import APP_VERSION

pytestmark = pytest.mark.e2e

# Таймауты: cold-start flutter-клиента и скан даже песочницы — секунды,
# но CI-машина/антивирус могут замедлить старт.
_SEMANTICS_TIMEOUT_MS = 90_000
_SCAN_TIMEOUT_MS = 60_000
_UI_TIMEOUT_MS = 30_000

# Дефолтный язык приложения — RU (settings.json в свежем data-dir отсутствует).
_RU_SCAN_BUTTON = "Начать поиск"
_RU_RESULTS_HEADER = "Результаты поиска"
_RU_NAV_SCANNER = "Поиск дубликатов"
_RU_NAV_SETTINGS = "Настройки"
_EN_NAV_SCANNER = "Duplicate Finder"

_DUP_NAMES = ("dup_alpha.txt", "dup_beta.txt", "dup_gamma.txt")
_UNIQUE_NAME = "unique_omega.txt"


def _enable_semantics(page) -> None:
    """Дождаться рендера и включить дерево flt-semantics (клик по placeholder)."""
    page.wait_for_selector("flt-semantics-placeholder, flt-semantics", timeout=_SEMANTICS_TIMEOUT_MS, state="attached")
    page.evaluate("() => { const el = document.querySelector('flt-semantics-placeholder'); if (el) { el.click(); return 'enabled'; } return 'already'; }")
    page.wait_for_selector("flt-semantics[role=button]", timeout=_SEMANTICS_TIMEOUT_MS, state="attached")


def _semantics_text(page) -> str:
    return page.evaluate("() => [...document.querySelectorAll('flt-semantics')].map(e => e.textContent || '').join('\\n')")


def _wait_for_text(page, text: str, timeout_ms: int = _UI_TIMEOUT_MS) -> None:
    page.wait_for_function("(t) => [...document.querySelectorAll('flt-semantics')].some(e => (e.textContent || '').includes(t))", arg=text, timeout=timeout_ms)


def _click_button(page, text: str, exact: bool = False) -> None:
    """JS-клик по flt-semantics[role=button] с кратчайшим совпадением текста."""
    result = page.evaluate(
        """([text, exact]) => {
            const els = [...document.querySelectorAll('flt-semantics[role=button]')]
                .filter(e => { const t = (e.textContent || '').trim(); return exact ? t === text : t.includes(text); });
            els.sort((a, b) => (a.textContent || '').length - (b.textContent || '').length);
            if (els.length) { els[0].click(); return 'clicked'; }
            return 'notfound';
        }""",
        [text, exact],
    )
    assert result == "clicked", f"Кнопка {text!r} не найдена в дереве семантики"


def _checkbox_stats(page) -> dict:
    """Чекбоксы файлов в результатах: role=checkbox + aria-checked."""
    return page.evaluate(
        """() => {
            const els = [...document.querySelectorAll('flt-semantics[role=checkbox]')];
            return {
                total: els.length,
                checked: els.filter(e => e.getAttribute('aria-checked') === 'true').length,
                attrs: els.map(e => e.getAttribute('aria-checked')),
            };
        }"""
    )


def _open_app(page, app_server) -> None:
    page.goto(app_server.url, timeout=_UI_TIMEOUT_MS)
    _enable_semantics(page)


def _run_scan_and_wait_results(page) -> None:
    """Клик «Начать поиск» (папка уже задана CLI-аргументом) -> экран результатов."""
    _click_button(page, _RU_SCAN_BUTTON)
    _wait_for_text(page, _RU_RESULTS_HEADER, timeout_ms=_SCAN_TIMEOUT_MS)


def test_e2e_smoke(page, app_server):
    """Страница загружается: заголовок TwinSweeper, сайдбар, версия из app_info."""
    _open_app(page, app_server)
    assert "TwinSweeper" in page.title()
    body = _semantics_text(page)
    assert APP_VERSION in body, f"Версия v{APP_VERSION} не найдена в UI"
    # Сайдбар: навигация (RU — язык по умолчанию для свежего data-dir).
    assert _RU_NAV_SCANNER in body
    assert _RU_NAV_SETTINGS in body
    # Экран поиска дубликатов активен: кнопка запуска скана на месте.
    assert _RU_SCAN_BUTTON in body


def test_e2e_scan_and_select(page, app_server):
    """Скан предзаданной песочницы: группа из 3 дубликатов, авто-выбор щадит первый файл.

    Папка задана программно (CLI-аргумент main.py) — нативный folder picker
    в web-режиме недоступен для автоматизации и не проверяется.
    """
    _open_app(page, app_server)
    _run_scan_and_wait_results(page)

    stats = _checkbox_stats(page)
    # Песочница: 3 идентичных txt (одна группа) + 1 уникальный (без пары).
    assert stats["total"] == 3, f"Ожидались 3 чекбокса группы дубликатов: {stats}"
    # Авто-выделение помечает все копии кроме первой: 2 выбрано, >=1 остался невыделенным.
    assert stats["checked"] == 2, f"Авто-выбор должен оставить >=1 файл невыделенным: {stats}"
    assert stats["total"] - stats["checked"] >= 1

    body = _semantics_text(page)
    for name in _DUP_NAMES:
        assert name in body, f"Файл {name} не показан в результатах"
    assert _UNIQUE_NAME not in body, "Уникальный файл не должен попадать в группы дубликатов"


def test_e2e_settings_persist(page, app_server):
    """Смена языка RU->EN не падает: результаты и выборка сохраняются, язык персистит."""
    _open_app(page, app_server)
    _run_scan_and_wait_results(page)
    before = _checkbox_stats(page)
    assert before["total"] == 3 and before["checked"] == 2, f"Предусловие: скан не дал ожидаемую выборку: {before}"

    _click_button(page, "EN", exact=True)
    # Сайдбар перерисовался на EN — приложение живо, сессия не упала.
    _wait_for_text(page, _EN_NAV_SCANNER, timeout_ms=_UI_TIMEOUT_MS)

    body = _semantics_text(page)
    # Открытые результаты переживают смену языка (текст вью — времени постройки, RU).
    assert _RU_RESULTS_HEADER in body, "Результаты сканирования потеряны после смены языка"
    after = _checkbox_stats(page)
    assert after == before, f"Выборка изменилась после смены языка: {before} -> {after}"

    # Настройка языка записана на диск (settings.json в DUPLICATER_DATA_DIR).
    settings_file = Path(app_server.data_dir) / "settings.json"
    deadline = time.monotonic() + 10
    settings = {}
    while time.monotonic() < deadline:
        if settings_file.exists():
            settings = json.loads(settings_file.read_text(encoding="utf-8"))
            if settings.get("language") == "en":
                break
        time.sleep(0.3)
    assert settings.get("language") == "en", f"Язык не персистирован в settings.json: {settings}"
