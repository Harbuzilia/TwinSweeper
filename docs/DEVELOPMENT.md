# Разработка TwinSweeper

Документ для контрибьюторов: как запустить проект из исходников, прогнать тесты,
собрать портативный EXE и где в коде что лежит.

## Требования

- Windows 10/11 (приложение использует `os.startfile`, `mbcs`, `ctypes kernel32`
  для хардлинков — на других ОС не запустится)
- Python 3.12+ (`os.path.isjunction`); проект собирается и тестируется на 3.14,
  CI гоняет 3.13
- Зависимости — в [`../requirements.txt`](../requirements.txt) (runtime) и
  [`../requirements-dev.txt`](../requirements-dev.txt) (pytest, ruff, pyinstaller,
  точные версии)

## Запуск из исходников

```bat
python -m venv venv
venv\Scripts\pip install -r requirements-dev.txt
venv\Scripts\python main.py
```

Или одной командой: `BUILD.bat` → пункт `[1] Run Application`.

В аргументах командной строки можно передать стартовые каталоги:

```bat
venv\Scripts\python main.py D:\Photos E:\Backup
```

## Тесты и линт

```bat
venv\Scripts\python -m pytest tests/ -q
venv\Scripts\python -m ruff check .
```

Базовая линия — **433 теста, все проходят**. Ожидаемое время прогона на
windows-latest: единицы секунд (реальная файловая система, без моков FS).

Тот же пайплайн выполняет CI: [`.github/workflows/ci.yml`](../.github/workflows/ci.yml)
(ruff → pytest, `windows-latest`, Python 3.13).

### Изоляция тестов от пользовательских данных

Ни один тест не сканирует, не меняет и не удаляет реальные файлы:

- `tests/conftest.py` создаёт временный каталог и выставляет переменную окружения
  `DUPLICATER_DATA_DIR` **до первого импорта проектных модулей** — поэтому
  `scan_cache.db`, `operations_log.json`, `scan_history.json` и лог всегда
  разрешаются внутрь временной папки, а не в профиль пользователя;
- файлы для проверок создаются только в `tmp_path` (фикстура `dup_tree` и т.п.);
- временный каталог удаляется на выходе интерпретатора (`atexit`).

Переопределение `DUPLICATER_DATA_DIR` — единственный поддерживаемый способ указать
другой каталог данных (см. `db_cache.get_data_dir()`). Имя переменной осталось
историческим: на него завязаны тесты и CI, а переименование тихо ломало бы изоляцию.

## Сборка портативного EXE

```bat
BUILD.bat
```

(пункт `[2] Build Portable EXE`)

Ручной запуск того же, что делает скрипт:

```bat
venv\Scripts\python -m pip install -r requirements-dev.txt
venv\Scripts\python -m PyInstaller --noconfirm TwinSweeper.spec
```

**Единственный источник сборки — [`../TwinSweeper.spec`](../TwinSweeper.spec)**:
в нём зафиксированы сборка `flet`, иконка окна (`assets/icon.ico` в `datas`,
иначе в `_MEIPASS` её не найти) и `upx=False` (сжатый неподписанный EXE даёт
заметно больше ложных срабатываний антивирусов). `BUILD.bat` вызывает именно
эту команду; отдельной CLI-командой (`--onefile --windowed --collect-all`)
проект больше не собирается — она расходилась со spec (терялись иконка и
`upx=False`).

Результат — `dist\TwinSweeper.exe`: работает на любом Windows-ПК без установленного
Python. Данные приложения в frozen-сборке уходят в `%LOCALAPPDATA%\TwinSweeper`
(в dev-режиме — каталог проекта).

## Структура проекта

```
main.py               — точка входа, окна/вкладки, фоновые воркеры
scanner.py            — сканирование, хэши, группы дубликатов, сравнение папок
phash_scanner.py      — перцептивные хэши изображений, кластеризация похожих
sweeper.py            — пустые папки, битые .lnk, файлы мусора
hardlink_manager.py   — NTFS-хардлинки с проверкой идентичности и откатом
db_cache.py           — потокобезопасный SQLite-кэш хэшей + get_data_dir()
ops_log.py            — журнал операций и отмена (delete/hardlink/move)
folder_priorities.py  — приоритетные папки для умного автовыделения
fs_filters.py         — общие фильтры обхода: junction'ы, $Recycle.Bin/SVI, inode-дедуп
locales.py            — RU/EN переводы (включая сообщения ядра)
app_info.py           — имя/версия приложения (единый источник)
app_logging.py        — файловое логирование (twinsweeper.log)
ui/                   — представления Flet (search, results, sample, compare, sweeper, thumbnails)
tests/                — pytest-тесты (изолированные, tmp_path) + CI (.github/workflows)
tools/make_icon.py    — генератор иконки приложения
```

## Связанные документы

- Безопасность данных и 10 гарантий корректности: [`SAFETY.md`](SAFETY.md)
- Аудит кода и статус найденных проблем: [`../AUDIT_REPORT.md`](../AUDIT_REPORT.md)
- История изменений: [`../CHANGELOG.md`](../CHANGELOG.md)
