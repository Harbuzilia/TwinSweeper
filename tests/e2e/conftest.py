"""Фикстуры e2e-гейта web-режима TwinSweeper.

Поднимают ``python main.py <sandbox>`` как subprocess с окружением:

* ``FLET_FORCE_WEB_SERVER=1`` + ``FLET_SERVER_PORT=<свободный порт>`` —
  web-режим Flet на известном порту (``ft.run`` читает эти переменные,
  когда порт не передан кодом);
* ``DUPLICATER_DATA_DIR=<tmp>`` — settings.json, scan_cache.db, журнал и
  лог приложения остаются во временной папке; пользовательские данные
  (корень проекта, %LOCALAPPDATA%) не читаются и не меняются.

Готовность определяется поллингом ``http://127.0.0.1:<port>/`` до HTTP 200;
после тестов процесс убивается вместе с деревом (``taskkill /T`` на Windows).

Браузер — системный Chrome (``launch(channel="chrome")``) с fallback на
bundled chromium Playwright: если ``playwright install`` не прошёл
(CDN/зеркало недоступны, chromium-1243 не скачался), гейт всё равно
запускается на системном браузере.

ВАЖНО про выбор папки сканирования: нативный folder picker
(``FilePicker.get_directory_path``) в web-режиме открывает системный диалог
браузера, который Playwright автоматизировать не может, — он в e2e НЕ
проверяется. Вместо этого папка задаётся программно через CLI-аргумент
``main.py``: ``sys.argv[1:]`` -> ``initial_cli_dirs`` ->
``SearchView.selected_directories`` (существующий механизм запуска
``python main.py D:\\Photos``, отдельный код под тесты не добавлялся).

Песочница сканирования — только tmp-каталог (3 одинаковых txt + 1 уникальный).
"""

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from collections.abc import Callable, Generator
from pathlib import Path

import pytest
from playwright.sync_api import Browser, Error as PlaywrightError

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Холодный старт Flet web-сервера + отдача flutter-клиента.
SERVER_READY_TIMEOUT_S = 120


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _tmp_root() -> str:
    """Корень для tmp-каталогов; страховка от падения temp в cwd (корень репо).

    Если ``tempfile.gettempdir()`` разрешается внутрь проекта (TEMP/TMP не
    заданы или невалидны — Python откатывается на cwd), берём пользовательский
    temp явно: прогоны e2e не должны оставлять каталоги-песочницы в репозитории.
    """
    root = Path(tempfile.gettempdir()).resolve()
    if root == PROJECT_ROOT or PROJECT_ROOT in root.parents:
        root = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "Temp"
        root.mkdir(parents=True, exist_ok=True)
    return str(root)


def _kill_tree(proc: subprocess.Popen) -> None:
    """Убить процесс и всех детей (flet может плодить дочерние процессы)."""
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
    else:
        proc.terminate()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:  # pragma: no cover - аварийная ветка
        proc.kill()
        proc.wait(timeout=10)


@pytest.fixture(scope="session")
def browser_context_args(browser_context_args):
    """Десктопный viewport: сайдбар 236px + контент должны влезать без скролла."""
    return {**browser_context_args, "viewport": {"width": 1600, "height": 900}}


@pytest.fixture(scope="session")
def browser(
    launch_browser: Callable[..., Browser],
    browser_type_launch_args: dict,
) -> Generator[Browser, None, None]:
    """Системный Chrome (``channel="chrome"``) с fallback на bundled chromium.

    Перекрывает одноимённую фикстуру pytest-playwright: бандл-браузер
    ставится отдельно (``playwright install``) и на этой машине не скачался
    (chromium-1243: CDN и зеркало прерваны), поэтому первым делом пробуем
    системный Chrome, при ошибке запуска — обычный ``launch()`` (bundled).
    Явный ``--browser-channel`` из CLI приоритетнее и не переопределяется.
    """
    if browser_type_launch_args.get("channel"):
        # Канал задан явно (CLI --browser-channel) — не вмешиваемся.
        browser = launch_browser()
    else:
        try:
            browser = launch_browser(channel="chrome")
        except PlaywrightError as exc:
            print(f"[e2e] launch(channel='chrome') не удался, fallback на bundled chromium: {exc}")
            browser = launch_browser()
    yield browser
    browser.close()


@pytest.fixture(scope="module")
def sandbox_dir():
    """Tmp-песочница: 3 байт-идентичных txt (группа дубликатов) + 1 уникальный."""
    d = Path(tempfile.mkdtemp(prefix="tws_e2e_sandbox_", dir=_tmp_root()))
    payload = "TwinSweeper e2e duplicate payload\n" * 20
    for name in ("dup_alpha.txt", "dup_beta.txt", "dup_gamma.txt"):
        (d / name).write_text(payload, encoding="utf-8")
    (d / "unique_omega.txt").write_text("single file without a pair\n", encoding="utf-8")
    yield d
    shutil.rmtree(d, ignore_errors=True)


class AppServer:
    """URL поднятого Flet web-сервера + его data-каталог (settings.json)."""

    def __init__(self, url: str, data_dir: Path):
        self.url = url
        self.data_dir = data_dir


@pytest.fixture(scope="module")
def app_server(sandbox_dir):
    """Один сервер на модуль: тесты идут последовательно (smoke -> scan -> settings)."""
    port = _free_port()
    data_dir = Path(tempfile.mkdtemp(prefix="tws_e2e_data_", dir=_tmp_root()))
    log_path = data_dir / "server_stdout.log"
    env = {
        **os.environ,
        "FLET_FORCE_WEB_SERVER": "1",
        "FLET_SERVER_PORT": str(port),
        "DUPLICATER_DATA_DIR": str(data_dir),
        "PYTHONIOENCODING": "utf-8",
    }
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    with open(log_path, "wb") as log_file:
        proc = subprocess.Popen(
            [sys.executable, "main.py", str(sandbox_dir)],
            cwd=str(PROJECT_ROOT),
            env=env,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
        )
        url = f"http://127.0.0.1:{port}/"
        deadline = time.monotonic() + SERVER_READY_TIMEOUT_S
        ready = False
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                break
            try:
                with urllib.request.urlopen(url, timeout=2) as resp:
                    if resp.status == 200:
                        ready = True
                        break
            except OSError:
                time.sleep(0.5)
        if not ready:
            tail = log_path.read_text(encoding="utf-8", errors="replace")[-3000:]
            _kill_tree(proc)
            pytest.fail(f"Flet web-сервер не поднялся на {url} (rc={proc.poll()}). Лог:\n{tail}")
        try:
            yield AppServer(url, data_dir)
        finally:
            _kill_tree(proc)
    shutil.rmtree(data_dir, ignore_errors=True)
