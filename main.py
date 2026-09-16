import flet as ft
import os
import sys
import json
import threading
from datetime import datetime
from typing import List, Dict

try:
    import winreg
    HAS_WINREG = True
except ImportError:
    HAS_WINREG = False

from scanner import scan_directory, scan_for_sample, compare_folders, FileInfo, format_file_size
from phash_scanner import scan_similar_images
from hardlink_manager import batch_replace_with_hardlinks
from db_cache import cache_db
from ops_log import load_operations, log_delete_operation, log_hardlink_operation, undo_hardlink_operation, get_data_dir

from ui.search_view import SearchView
from ui.results_view import ResultsView
from ui.sample_search_view import SampleSearchView
from ui.compare_view import CompareView
from ui.sweeper_view import SweeperView
from ui.components import (
    get_styled_card, get_stat_card, get_primary_button, get_outlined_button, get_header_row, get_badge,
    get_styled_dialog, get_action_icon_button,
    set_active_theme, get_active_theme_key, get_current_theme,
    BG_COLOR, SURFACE_COLOR, SURFACE_HOVER, BORDER_COLOR,
    PRIMARY_COLOR, ACCENT_COLOR, SUCCESS_COLOR, WARNING_COLOR, DANGER_COLOR,
    TEXT_PRIMARY, TEXT_SECONDARY, TEXT_MUTED
)
from locales import get_text

try:
    from send2trash import send2trash
    HAS_SEND2TRASH = True
except ImportError:
    HAS_SEND2TRASH = False

HISTORY_FILE = os.path.join(get_data_dir(), "scan_history.json")

def load_history() -> List[dict]:
    try:
        if os.path.exists(HISTORY_FILE):
            with open(HISTORY_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception:
        pass
    return []

def save_history(history: List[dict]):
    try:
        with open(HISTORY_FILE, 'w', encoding='utf-8') as f:
            json.dump(history[-30:], f, ensure_ascii=False, indent=2)
    except Exception:
        pass

def add_to_history(directories: List[str], duplicates_found: int, wasted_space: int):
    history = load_history()
    history.append({
        "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "folders": directories,
        "duplicates": duplicates_found,
        "wasted": wasted_space
    })
    save_history(history)

# Windows Context Menu Integration Helpers
def is_context_menu_registered() -> bool:
    if not HAS_WINREG:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Classes\Directory\shell\Duplicater"):
            return True
    except OSError:
        return False

def register_context_menu(app_title: str = "Найти дубликаты в Duplicater"):
    if not HAS_WINREG:
        return False, "winreg not available"
    try:
        if getattr(sys, 'frozen', False):
            cmd = f'"{sys.executable}" "%1"'
            bg_cmd = f'"{sys.executable}" "%V"'
        else:
            main_py = os.path.abspath(os.path.join(os.path.dirname(__file__), "main.py"))
            python_exe = sys.executable.replace("python.exe", "pythonw.exe") if os.path.exists(sys.executable.replace("python.exe", "pythonw.exe")) else sys.executable
            cmd = f'"{python_exe}" "{main_py}" "%1"'
            bg_cmd = f'"{python_exe}" "{main_py}" "%V"'

        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Classes\Directory\shell\Duplicater") as key:
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, app_title)
            winreg.SetValueEx(key, "Icon", 0, winreg.REG_SZ, sys.executable)
            with winreg.CreateKey(key, "command") as cmd_key:
                winreg.SetValueEx(cmd_key, "", 0, winreg.REG_SZ, cmd)

        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Classes\Directory\Background\shell\Duplicater") as key:
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, app_title)
            winreg.SetValueEx(key, "Icon", 0, winreg.REG_SZ, sys.executable)
            with winreg.CreateKey(key, "command") as cmd_key:
                winreg.SetValueEx(cmd_key, "", 0, winreg.REG_SZ, bg_cmd)
        return True, ""
    except Exception as ex:
        return False, str(ex)

def unregister_context_menu():
    if not HAS_WINREG:
        return False, "winreg not available"
    try:
        for subkey in [
            r"Software\Classes\Directory\shell\Duplicater\command",
            r"Software\Classes\Directory\shell\Duplicater",
            r"Software\Classes\Directory\Background\shell\Duplicater\command",
            r"Software\Classes\Directory\Background\shell\Duplicater"
        ]:
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, subkey)
            except OSError:
                pass
        return True, ""
    except Exception as ex:
        return False, str(ex)

def main(page: ft.Page):
    # App Window & Appearance
    page.title = "Duplicater"
    page.theme_mode = ft.ThemeMode.DARK
    page.bgcolor = BG_COLOR
    page.padding = 0
    page.theme = ft.Theme(
        font_family="Segoe UI",
        scrollbar_theme=ft.ScrollbarTheme(
            thumb_visibility=True,
            thickness=6,
            radius=4,
        )
    )
    current_language = "ru"
    current_nav_index = 0
    cancel_flag = [False]

    # CLI Directory Arguments
    initial_cli_dirs = []
    for arg in sys.argv[1:]:
        if os.path.exists(arg) and os.path.isdir(arg):
            initial_cli_dirs.append(os.path.abspath(arg))

    search_view_instance = None
    results_view_instance = None

    def set_language(lang: str):
        nonlocal current_language
        current_language = lang
        page.title = get_text("app_title", lang)
        on_nav_change(current_nav_index)

    def change_theme(theme_name: str):
        set_active_theme(theme_name)
        theme_colors = get_current_theme()
        page.bgcolor = theme_colors["BG_COLOR"]
        page.theme_mode = ft.ThemeMode.LIGHT if theme_name == "light_clean" else ft.ThemeMode.DARK
        page.theme = ft.Theme(
            font_family="Segoe UI",
            scrollbar_theme=ft.ScrollbarTheme(
                thumb_visibility=True,
                thickness=6,
                radius=4,
            )
        )
        on_nav_change(current_nav_index)

    def cancel_scan():
        cancel_flag[0] = True

    # Scan Runners
    def run_scan(
        directories: List[str],
        by_name: bool,
        by_size: bool,
        by_hash: bool,
        by_byte: bool,
        progress_callback,
        on_cancel_setup=None,
        exclude_patterns=None,
        turbo_mode=True,
        min_size_bytes=0,
        ignore_empty_files=True,
        on_scan_finished=None,
        is_phash=False,
        phash_threshold=0.90
    ):
        cancel_flag[0] = False
        if on_cancel_setup:
            on_cancel_setup(cancel_scan)

        def _worker():
            results = {}
            try:
                if is_phash:
                    results = scan_similar_images(
                        directories=directories,
                        similarity_threshold=phash_threshold,
                        progress_callback=progress_callback,
                        cancel_flag=cancel_flag,
                        exclude_patterns=exclude_patterns
                    )
                else:
                    results = scan_directory(
                        directories=directories,
                        by_name=by_name,
                        by_size=by_size,
                        by_hash=by_hash,
                        by_byte=by_byte,
                        progress_callback=progress_callback,
                        cancel_flag=cancel_flag,
                        exclude_patterns=exclude_patterns,
                        turbo_mode=turbo_mode,
                        min_size_bytes=min_size_bytes,
                        ignore_empty_files=ignore_empty_files
                    )
            except Exception as ex:
                progress_callback(f"Error: {ex}", None)
            finally:
                if on_scan_finished:
                    on_scan_finished()

            if cancel_flag[0]:
                progress_callback(get_text("scan_cancelled", current_language), None)
            elif not results:
                progress_callback(get_text("no_duplicates", current_language), None)
            else:
                total_dupes = sum(max(0, len(files) - 1) for files in results.values())
                total_wasted = sum(max(0, len(files) - 1) * files[0].size for files in results.values() if files)
                add_to_history(directories, total_dupes, total_wasted)
                show_results_screen(results)

        threading.Thread(target=_worker, daemon=True).start()

    def run_sample_scan(
        sample_path: str,
        directories: List[str],
        by_name: bool,
        by_size: bool,
        by_hash: bool,
        by_byte: bool,
        progress_callback,
        on_cancel_setup=None,
        on_scan_finished=None
    ):
        cancel_flag[0] = False
        if on_cancel_setup:
            on_cancel_setup(cancel_scan)

        def _worker():
            found_files = []
            try:
                found_files = scan_for_sample(
                    sample_path=sample_path,
                    search_directories=directories,
                    by_name=by_name,
                    by_size=by_size,
                    by_hash=by_hash,
                    by_byte=by_byte,
                    progress_callback=progress_callback,
                    cancel_flag=cancel_flag
                )
            except Exception as ex:
                progress_callback(f"Error: {ex}", None)
            finally:
                if on_scan_finished:
                    on_scan_finished()

            if cancel_flag[0]:
                progress_callback(get_text("scan_cancelled", current_language), None)
                return

            if found_files:
                try:
                    s_stat = os.stat(sample_path)
                    sample_info = FileInfo(sample_path, os.path.basename(sample_path), s_stat.st_size, s_stat.st_ctime, s_stat.st_mtime)
                    results = {f"Sample: {sample_info.name}": [sample_info] + found_files}
                    show_results_screen(results)
                except OSError:
                    progress_callback(get_text("no_duplicates", current_language), None)
            else:
                progress_callback(get_text("no_duplicates", current_language), None)

        threading.Thread(target=_worker, daemon=True).start()

    def run_compare(folder_a: str, folder_b: str, result_callback, progress_callback):
        def _worker():
            results = None
            try:
                results = compare_folders(folder_a, folder_b, progress_callback)
            except Exception as ex:
                progress_callback(f"Error: {ex}", None)
            finally:
                result_callback(results or {"only_in_a": [], "only_in_b": [], "identical": [], "modified": []})

        threading.Thread(target=_worker, daemon=True).start()

    # View Transition Handlers
    def show_results_screen(results: Dict[str, List[FileInfo]]):
        nonlocal results_view_instance
        def on_back():
            on_nav_change(0)
            if search_view_instance:
                search_view_instance.reset_state()

        results_view_instance = ResultsView(
            results=results,
            on_back=on_back,
            on_delete=delete_files_handler,
            on_hardlink=hardlink_files_handler,
            language=current_language
        )
        main_content_container.content = results_view_instance
        page.update()

    def delete_files_handler(file_paths: List[str], use_trash: bool = True):
        total = len(file_paths)
        state = {"deleted_count": 0, "total_freed": 0, "errors": [], "actually_deleted": []}

        def _worker():
            CHUNK = 200
            for start in range(0, total, CHUNK):
                chunk = file_paths[start:start + CHUNK]
                for path in chunk:
                    try:
                        size = os.path.getsize(path) if os.path.exists(path) else 0
                        if use_trash and HAS_SEND2TRASH:
                            try:
                                send2trash(path)
                            except Exception:
                                os.remove(path)
                        else:
                            os.remove(path)
                        state["deleted_count"] += 1
                        state["total_freed"] += size
                        state["actually_deleted"].append(path)
                    except Exception as ex:
                        state["errors"].append(get_text("error_delete", current_language).format(path, ex))

                # Update progress
                done = min(start + CHUNK, total)
                if results_view_instance:
                    results_view_instance.operation_progress.value = done / total
                    results_view_instance.operation_status.value = get_text("deleting", current_language).format(state["deleted_count"], total)
                    try:
                        page.update()
                    except Exception:
                        pass

            # Hide progress
            if results_view_instance:
                results_view_instance.operation_progress.visible = False
                results_view_instance.operation_status.visible = False

            if results_view_instance and state["actually_deleted"]:
                results_view_instance.remove_files(state["actually_deleted"])

            if state["actually_deleted"]:
                log_delete_operation(state["actually_deleted"], state["total_freed"], use_trash)

            msg = get_text("deleted_count", current_language).format(state["deleted_count"])
            msg += "\n" + get_text("deleted_space_freed", current_language).format(format_file_size(state["total_freed"]))
            if state["errors"]:
                msg += "\n\nErrors:\n" + "\n".join(state["errors"][:5])

            dlg = get_styled_dialog(
                title=get_text("deletion_complete", current_language),
                title_color=SUCCESS_COLOR,
                content=ft.Text(msg, size=13, color=TEXT_SECONDARY),
                actions=[
                    get_primary_button(text=get_text("ok", current_language), on_click=lambda _: page.pop_dialog(), height=36)
                ]
            )
            page.show_dialog(dlg)
            try:
                page.update()
            except Exception:
                pass

        threading.Thread(target=_worker, daemon=True).start()

    def hardlink_files_handler(groups_map: Dict[str, List[str]]):
        total_dupes = sum(len(dups) for dups in groups_map.values())
        state = {"success_count": 0, "freed_bytes": 0, "errors": [], "succeeded_paths": []}

        def _worker():
            CHUNK = 200
            items = [(orig, dup) for orig, dups in groups_map.items() for dup in dups]
            processed = 0
            for start in range(0, len(items), CHUNK):
                chunk = items[start:start + CHUNK]
                chunk_map = {}
                for orig, dup in chunk:
                    chunk_map.setdefault(orig, []).append(dup)
                sc, fb, errs, sp = batch_replace_with_hardlinks(chunk_map)
                state["success_count"] += sc
                state["freed_bytes"] += fb
                state["errors"].extend(errs)
                state["succeeded_paths"].extend(sp)
                processed += len(chunk)

                # Update progress
                if results_view_instance:
                    results_view_instance.operation_progress.value = processed / total_dupes if total_dupes else 1
                    results_view_instance.operation_status.value = get_text("hardlinking", current_language).format(state["success_count"], total_dupes)
                    try:
                        page.update()
                    except Exception:
                        pass

            # Hide progress
            if results_view_instance:
                results_view_instance.operation_progress.visible = False
                results_view_instance.operation_status.visible = False

            # Remove only files that were actually replaced; failed ones stay selectable.
            if results_view_instance and state["succeeded_paths"]:
                results_view_instance.remove_files(state["succeeded_paths"])

            if state["succeeded_paths"]:
                succeeded_set = set(state["succeeded_paths"])
                pairs = [(orig, dup) for orig, dups in groups_map.items() for dup in dups if dup in succeeded_set]
                if pairs:
                    log_hardlink_operation(pairs, state["freed_bytes"])

            msg = get_text("hardlink_success_msg", current_language).format(state["success_count"])
            msg += "\n" + get_text("hardlink_space_saved", current_language).format(format_file_size(state["freed_bytes"]))
            if state["errors"]:
                msg += "\n\nWarnings/Errors:\n" + "\n".join(state["errors"][:4])

            dlg = get_styled_dialog(
                title=get_text("hardlink_complete", current_language),
                title_color=PRIMARY_COLOR,
                content=ft.Text(msg, size=13, color=TEXT_SECONDARY),
                actions=[
                    get_primary_button(text=get_text("ok", current_language), on_click=lambda _: page.pop_dialog(), height=36)
                ]
            )
            page.show_dialog(dlg)
            try:
                page.update()
            except Exception:
                pass

        threading.Thread(target=_worker, daemon=True).start()

    def rescan_history_entry(folders: List[str]):
        on_nav_change(0)
        if search_view_instance:
            search_view_instance.selected_directories = [f for f in folders if os.path.exists(f)]
            search_view_instance.update_folders_list()

    # Sub-views Builders
    def build_history_tab():
        history = load_history()
        if not history:
            return ft.Container(
                content=ft.Column([
                    ft.Icon(ft.Icons.HISTORY_ROUNDED, size=48, color=TEXT_MUTED),
                    ft.Text(get_text("no_history", current_language), size=14, color=TEXT_MUTED),
                ], alignment=ft.MainAxisAlignment.CENTER, horizontal_alignment=ft.CrossAxisAlignment.CENTER),
                alignment=ft.Alignment.CENTER,
                expand=True
            )

        total_analyzed = sum(item.get("wasted", 0) for item in history)
        history_items = []

        def clear_history_action(e):
            save_history([])
            on_nav_change(4)

        for idx, item in enumerate(reversed(history)):
            real_idx = len(history) - 1 - idx
            folders_list = item.get("folders", [])
            wasted_str = format_file_size(item.get("wasted", 0))

            def delete_entry(i=real_idx):
                h = load_history()
                if 0 <= i < len(h):
                    h.pop(i)
                    save_history(h)
                    on_nav_change(4)

            history_items.append(
                get_styled_card(
                    ft.Row([
                        ft.Container(
                            content=ft.Icon(ft.Icons.SCHEDULE_ROUNDED, color=PRIMARY_COLOR, size=20),
                            bgcolor=f"{PRIMARY_COLOR}18",
                            border_radius=8,
                            padding=8
                        ),
                        ft.Column([
                            ft.Text(item.get("date", ""), weight=ft.FontWeight.BOLD, size=13, color=TEXT_PRIMARY),
                            ft.Text(", ".join(folders_list[:2]) + ("..." if len(folders_list) > 2 else ""), size=12, color=TEXT_MUTED),
                        ], expand=True, spacing=2),
                        ft.Column([
                            get_badge(f"{item.get('duplicates', 0)} {get_text('files', current_language)}", color=PRIMARY_COLOR),
                            get_badge(f"-{wasted_str}", color=DANGER_COLOR),
                        ], horizontal_alignment=ft.CrossAxisAlignment.END, spacing=4),
                        get_primary_button(
                            text=get_text("rescan_now", current_language),
                            on_click=lambda _, f=folders_list: rescan_history_entry(f),
                            icon=ft.Icons.REFRESH_ROUNDED,
                            height=34
                        ),
                        get_action_icon_button(
                            icon=ft.Icons.DELETE_OUTLINE_ROUNDED,
                            icon_color=DANGER_COLOR,
                            tooltip=get_text("delete", current_language),
                            on_click=lambda _, i=real_idx: delete_entry(i),
                            button_size=34,
                            icon_size=18
                        )
                    ], vertical_alignment=ft.CrossAxisAlignment.CENTER, spacing=12),
                    padding=12
                )
            )

        operations = load_operations()
        ops_items = []

        def undo_hardlink_action(op_id: str):
            restored, undo_errors = undo_hardlink_operation(op_id)
            undo_msg = get_text("op_undo_done", current_language).format(restored)
            if undo_errors:
                undo_msg += "\n" + "\n".join(undo_errors[:3])
            undo_dlg = get_styled_dialog(
                title=get_text("ops_log_title", current_language),
                title_color=PRIMARY_COLOR,
                content=ft.Text(undo_msg, size=13, color=TEXT_SECONDARY),
                actions=[get_primary_button(text=get_text("ok", current_language), on_click=lambda _: page.pop_dialog(), height=36)]
            )
            page.show_dialog(undo_dlg)
            on_nav_change(4)

        for op in operations[:10]:
            op_icon = ft.Icons.LINK_ROUNDED if op.get("type") == "hardlink" else ft.Icons.DELETE_OUTLINE_ROUNDED
            op_color = PRIMARY_COLOR if op.get("type") == "hardlink" else DANGER_COLOR
            op_title_key = "op_hardlink" if op.get("type") == "hardlink" else "op_delete"
            op_time = datetime.fromtimestamp(op.get("ts", 0)).strftime("%Y-%m-%d %H:%M")
            op_row_controls = [
                ft.Container(
                    content=ft.Icon(op_icon, color=op_color, size=16),
                    bgcolor=f"{op_color}18",
                    border_radius=6,
                    padding=6
                ),
                ft.Column([
                    ft.Text(get_text(op_title_key, current_language).format(len(op.get("paths", []))), weight=ft.FontWeight.BOLD, size=13, color=TEXT_PRIMARY),
                    ft.Text(f"{op_time}  •  -{format_file_size(op.get('freed_bytes', 0))}", size=11, color=TEXT_MUTED),
                ], expand=True, spacing=2),
            ]
            if op.get("type") == "hardlink" and not op.get("undone"):
                op_row_controls.append(get_action_icon_button(icon=ft.Icons.UNDO_ROUNDED, icon_color=WARNING_COLOR, tooltip=get_text("op_undo", current_language), on_click=lambda _, oid=op.get("id"): undo_hardlink_action(oid), button_size=34, icon_size=18))
            elif op.get("undone"):
                op_row_controls.append(get_badge(get_text("op_undone", current_language), color=TEXT_MUTED))
            ops_items.append(ft.Container(
                content=ft.Row(op_row_controls, vertical_alignment=ft.CrossAxisAlignment.CENTER, spacing=10),
                bgcolor=SURFACE_HOVER,
                border_radius=8,
                padding=ft.Padding.symmetric(horizontal=10, vertical=6)
            ))
        ops_section = ft.Container()
        if ops_items:
            ops_section = get_styled_card(
                ft.Column([
                    ft.Text(get_text("ops_log_title", current_language), size=13, weight=ft.FontWeight.BOLD, color=TEXT_PRIMARY),
                    ft.Column(ops_items, spacing=6),
                ], spacing=8),
                padding=12
            )

        return ft.Column([
            get_header_row(
                title=get_text("history", current_language),
                subtitle=get_text("history_desc", current_language),
                action_control=get_outlined_button(
                    text=get_text("clear_history", current_language),
                    icon=ft.Icons.DELETE_SWEEP_ROUNDED,
                    on_click=clear_history_action,
                    color=DANGER_COLOR,
                    border_color=DANGER_COLOR,
                    height=36
                )
            ),
            ft.Row([
                get_stat_card(
                    title=get_text("total_freed_all_time", current_language),
                    value=format_file_size(total_analyzed),
                    subtitle=get_text("scan_runs_recorded", current_language).format(len(history)),
                    icon=ft.Icons.AUTO_AWESOME_ROUNDED,
                    icon_color=ACCENT_COLOR
                )
            ]),
            ops_section,
            ft.Column(history_items, scroll=ft.ScrollMode.AUTO, expand=True, spacing=8)
        ], spacing=16, expand=True)

    def build_settings_tab():
        cache_stats = cache_db.get_cache_stats()
        cache_label = ft.Text(
            get_text("cache_stats_label", current_language).format(
                cache_stats["total_files"] + cache_stats["total_images"],
                int(cache_stats["size_bytes"] / 1024)
            ),
            size=13,
            color=TEXT_MUTED
        )

        def on_clear_cache(e):
            cache_db.clear_cache()
            cache_label.value = get_text("cache_stats_label", current_language).format(0, 0)
            cache_label.update()

            dlg = get_styled_dialog(
                title=get_text("cache_cleared", current_language),
                title_color=SUCCESS_COLOR,
                actions=[get_primary_button(text=get_text("ok", current_language), on_click=lambda _: page.pop_dialog(), height=36)]
            )
            page.show_dialog(dlg)

        # Context Menu State
        is_registered = is_context_menu_registered()
        context_status_text = ft.Text(
            get_text("context_menu_desc", current_language),
            size=12,
            color=TEXT_MUTED
        )

        def toggle_context_menu(e):
            if is_context_menu_registered():
                ok, err = unregister_context_menu()
                msg = get_text("context_menu_removed", current_language) if ok else f"Error: {err}"
            else:
                ok, err = register_context_menu("Найти дубликаты в Duplicater" if current_language == "ru" else "Scan Duplicates with Duplicater")
                msg = get_text("context_menu_added", current_language) if ok else f"Error: {err}"

            dlg = get_styled_dialog(
                title=msg,
                title_color=SUCCESS_COLOR if ok else DANGER_COLOR,
                actions=[get_primary_button(text=get_text("ok", current_language), on_click=lambda _: page.pop_dialog(), height=36)]
            )
            page.show_dialog(dlg)
            on_nav_change(0)

        return ft.Column([
            get_header_row(
                title=get_text("settings_title", current_language),
                subtitle=get_text("settings_desc", current_language)
            ),
            get_styled_card(
                ft.Column([
                    # Language
                    ft.Text(get_text("language_setting", current_language), size=13, weight=ft.FontWeight.W_600, color=TEXT_PRIMARY),
                    ft.Dropdown(
                        width=240,
                        value=current_language,
                        border_color=BORDER_COLOR,
                        bgcolor=SURFACE_HOVER,
                        border_radius=8,
                        text_size=13,
                        options=[
                            ft.dropdown.Option("ru", "Русский (RU)"),
                            ft.dropdown.Option("en", "English (EN)"),
                        ],
                        on_select=lambda e: set_language(e.control.value)
                    ),
                    ft.Divider(color=BORDER_COLOR, height=20),

                    # Visual Theme Switcher
                    ft.Text(get_text("theme_setting", current_language), size=13, weight=ft.FontWeight.W_600, color=TEXT_PRIMARY),
                    (lambda: (
                        ft.Row([
                            ft.Container(
                                content=ft.Row([
                                    ft.Container(width=16, height=16, border_radius=8, bgcolor=preview_bg, border=ft.Border.all(1, preview_border)),
                                    ft.Text(get_text(t_key, current_language), size=13, weight=ft.FontWeight.W_600 if get_active_theme_key() == t_id else ft.FontWeight.NORMAL, color=TEXT_PRIMARY)
                                ], spacing=10, vertical_alignment=ft.CrossAxisAlignment.CENTER),
                                padding=ft.Padding.symmetric(horizontal=14, vertical=10),
                                border_radius=10,
                                bgcolor=SURFACE_HOVER if get_active_theme_key() == t_id else ft.Colors.TRANSPARENT,
                                border=ft.Border.all(1.5, PRIMARY_COLOR if get_active_theme_key() == t_id else BORDER_COLOR),
                                on_click=lambda _, tid=t_id: change_theme(tid),
                                ink=True
                            )
                            for t_id, t_key, preview_bg, preview_border in [
                                ("dark_slate", "theme_slate", "#0B0E14", "#2A324B"),
                                ("midnight_oled", "theme_oled", "#000000", "#1A1A1A"),
                                ("light_clean", "theme_light", "#F8FAFC", "#CBD5E1"),
                            ]
                        ], spacing=12, wrap=True)
                    ))(),
                    ft.Divider(color=BORDER_COLOR, height=20),

                    # Recycle bin
                    ft.Checkbox(
                        label=get_text("default_trash_setting", current_language),
                        value=True,
                        disabled=not HAS_SEND2TRASH
                    ),
                    ft.Divider(color=BORDER_COLOR, height=20),

                    # Windows Explorer Context Menu
                    ft.Text(get_text("context_menu_section", current_language), size=13, weight=ft.FontWeight.W_600, color=TEXT_PRIMARY),
                    context_status_text,
                    get_outlined_button(
                        text=get_text("remove_context_menu_btn" if is_registered else "add_context_menu_btn", current_language),
                        on_click=toggle_context_menu,
                        icon=ft.Icons.APPS_OUTLINED if not is_registered else ft.Icons.DELETE_OUTLINE_ROUNDED,
                        border_color=DANGER_COLOR if is_registered else PRIMARY_COLOR,
                        color=DANGER_COLOR if is_registered else PRIMARY_COLOR,
                        height=36
                    ),
                    ft.Divider(color=BORDER_COLOR, height=20),

                    # SQLite Cache section
                    ft.Text(get_text("cache_section", current_language), size=13, weight=ft.FontWeight.W_600, color=TEXT_PRIMARY),
                    cache_label,
                    get_outlined_button(
                        text=get_text("clear_cache_btn", current_language),
                        on_click=on_clear_cache,
                        icon=ft.Icons.CLEANING_SERVICES_ROUNDED,
                        border_color=WARNING_COLOR,
                        color=WARNING_COLOR,
                        height=36
                    )
                ], spacing=10),
                padding=20
            )
        ], spacing=16, expand=True)

    # Shell Layout Navigation
    main_content_container = ft.Container(padding=20, expand=True)

    def on_nav_change(index: int):
        nonlocal current_nav_index, search_view_instance, initial_cli_dirs
        current_nav_index = index

        if index == 0:
            # Carry the user's folder selection across tab switches; CLI-provided
            # folders are applied exactly once on the first visit.
            previous_dirs = list(search_view_instance.selected_directories) if search_view_instance else []
            search_view_instance = SearchView(
                on_scan_start=run_scan,
                language=current_language,
                on_language_change=set_language
            )
            if initial_cli_dirs:
                search_view_instance.selected_directories = list(initial_cli_dirs)
                initial_cli_dirs = []
            elif previous_dirs:
                search_view_instance.selected_directories = previous_dirs
            if search_view_instance.selected_directories:
                search_view_instance.update_folders_list()
            main_content_container.content = search_view_instance
        elif index == 1:
            main_content_container.content = SweeperView(
                language=current_language
            )
        elif index == 2:
            main_content_container.content = SampleSearchView(
                on_scan_start=run_sample_scan,
                language=current_language
            )
        elif index == 3:
            main_content_container.content = CompareView(
                on_compare_start=run_compare,
                language=current_language
            )
        elif index == 4:
            main_content_container.content = build_history_tab()
        elif index == 5:
            main_content_container.content = build_settings_tab()

        render_app_shell()
        page.update()

    def build_sidebar() -> ft.Container:
        theme = get_current_theme()
        nav_destinations = [
            (get_text("nav_scanner", current_language), ft.Icons.SEARCH_ROUNDED),
            (get_text("nav_sweeper", current_language), ft.Icons.CLEANING_SERVICES_ROUNDED),
            (get_text("nav_sample", current_language), ft.Icons.FINGERPRINT_ROUNDED),
            (get_text("nav_compare", current_language), ft.Icons.COMPARE_ARROWS_ROUNDED),
            (get_text("nav_history", current_language), ft.Icons.HISTORY_ROUNDED),
            (get_text("nav_settings", current_language), ft.Icons.SETTINGS_OUTLINED),
        ]

        nav_buttons = []
        for idx, (label, icon_name) in enumerate(nav_destinations):
            is_selected = (idx == current_nav_index)
            btn = ft.Container(
                content=ft.Row([
                    ft.Container(
                        width=3,
                        height=20,
                        border_radius=2,
                        bgcolor=theme["PRIMARY_COLOR"] if is_selected else "transparent",
                    ),
                    ft.Icon(icon_name, size=19, color=theme["PRIMARY_COLOR"] if is_selected else theme["TEXT_SECONDARY"]),
                    ft.Text(
                        label,
                        size=13,
                        weight=ft.FontWeight.W_600 if is_selected else ft.FontWeight.W_500,
                        color=theme["TEXT_PRIMARY"] if is_selected else theme["TEXT_SECONDARY"],
                        expand=True
                    )
                ], spacing=10, vertical_alignment=ft.CrossAxisAlignment.CENTER),
                bgcolor=f"{theme['PRIMARY_COLOR']}1C" if is_selected else "transparent",
                border=ft.Border.all(1, f"{theme['PRIMARY_COLOR']}44") if is_selected else ft.Border.all(1, "transparent"),
                border_radius=10,
                padding=ft.Padding.symmetric(horizontal=8, vertical=9),
                on_click=lambda _, i=idx: on_nav_change(i),
                ink=True
            )
            nav_buttons.append(btn)

        # Brand Header & Language Switcher
        return ft.Container(
            content=ft.Column([
                ft.Container(
                    content=ft.Row([
                        ft.Container(
                            content=ft.Icon(ft.Icons.COPY_ALL_ROUNDED, color=ft.Colors.WHITE, size=22),
                            bgcolor=theme["PRIMARY_COLOR"],
                            border_radius=10,
                            padding=8,
                            shadow=ft.BoxShadow(
                                blur_radius=8,
                                spread_radius=0,
                                color=f"{theme['PRIMARY_COLOR']}55",
                                offset=ft.Offset(0, 2)
                            )
                        ),
                        ft.Column([
                            ft.Text("DUPLICATER", size=15, weight=ft.FontWeight.BOLD, color=theme["TEXT_PRIMARY"]),
                            ft.Text("Pro Disk Optimizer", size=11, color=theme["TEXT_MUTED"]),
                        ], spacing=1)
                    ], spacing=12, vertical_alignment=ft.CrossAxisAlignment.CENTER),
                    padding=ft.Padding.only(bottom=10, top=4, left=4, right=4)
                ),
                ft.Divider(color=theme["BORDER_COLOR"], height=1),
                
                ft.Column(nav_buttons, spacing=4, expand=True),

                ft.Divider(color=theme["BORDER_COLOR"], height=1),
                ft.Container(
                    content=ft.Row([
                        ft.Container(
                            content=ft.Row([
                                ft.Container(
                                    content=ft.Text("RU", size=12, weight=ft.FontWeight.BOLD if current_language == "ru" else ft.FontWeight.NORMAL, color="#FFFFFF" if current_language == "ru" else theme["TEXT_MUTED"]),
                                    bgcolor=theme["PRIMARY_COLOR"] if current_language == "ru" else "transparent",
                                    border_radius=6,
                                    padding=ft.Padding.symmetric(horizontal=10, vertical=5),
                                    on_click=lambda _: set_language("ru"),
                                    ink=True
                                ),
                                ft.Container(
                                    content=ft.Text("EN", size=12, weight=ft.FontWeight.BOLD if current_language == "en" else ft.FontWeight.NORMAL, color="#FFFFFF" if current_language == "en" else theme["TEXT_MUTED"]),
                                    bgcolor=theme["PRIMARY_COLOR"] if current_language == "en" else "transparent",
                                    border_radius=6,
                                    padding=ft.Padding.symmetric(horizontal=10, vertical=5),
                                    on_click=lambda _: set_language("en"),
                                    ink=True
                                ),
                            ], spacing=2),
                            bgcolor=theme["SURFACE_CARD"],
                            border=ft.Border.all(1, theme["BORDER_COLOR"]),
                            border_radius=8,
                            padding=2
                        ),
                        ft.Container(expand=True),
                        ft.Container(
                            content=ft.Text("v3.5", size=11, color=theme["TEXT_MUTED"], weight=ft.FontWeight.W_500),
                            padding=ft.Padding.only(right=4)
                        )
                    ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN, vertical_alignment=ft.CrossAxisAlignment.CENTER),
                    padding=ft.Padding.only(top=4, bottom=4)
                )
            ], spacing=8),
            width=236,
            bgcolor=theme["SURFACE_COLOR"],
            border=ft.Border.only(right=ft.BorderSide(1, theme["BORDER_COLOR"])),
            padding=ft.Padding.symmetric(horizontal=14, vertical=16)
        )

    def render_app_shell():
        sidebar = build_sidebar()
        page.clean()
        page.add(
            ft.Row([
                sidebar,
                main_content_container
            ], expand=True, spacing=0)
        )

    on_nav_change(0)

if __name__ == "__main__":
    ft.run(main)
