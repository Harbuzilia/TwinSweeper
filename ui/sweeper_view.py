import flet as ft
import os
import threading
from typing import List, Dict, Set

from sweeper import find_empty_directories, delete_empty_directories, find_broken_shortcuts, find_junk_files
from scanner import FileInfo, format_file_size, verify_file_unchanged
from ui.components import (
    get_styled_card, get_styled_dialog, get_primary_button, get_outlined_button, get_header_row, get_badge,
    get_segmented_control, get_drive_chip, get_folder_list_item, get_progress_card,
    get_detected_drives,
    PRIMARY_COLOR, SUCCESS_COLOR, WARNING_COLOR, DANGER_COLOR,
    TEXT_PRIMARY, TEXT_SECONDARY, TEXT_MUTED, SURFACE_HOVER, BORDER_COLOR
)
from locales import get_text
from ops_log import log_delete_operation

try:
    from send2trash import send2trash
    HAS_SEND2TRASH = True
except ImportError:
    HAS_SEND2TRASH = False

# Junk pre-selection policy (round 3): backup/dump extensions and large files
# are often deliberate user data (DB dumps, game saves, app backups). They are
# listed but NOT pre-checked, so a one-click "Clean" cannot sweep them away.
_JUNK_NO_PRESELECT_EXT = {".bak", ".dmp", ".old"}
_JUNK_PRESELECT_MAX_BYTES = 100 * 1024 * 1024  # 100 MB

class SweeperView(ft.Column):
    ITEMS_PER_PAGE = 50

    def __init__(self, on_delete_files=None, language="ru", trash_default: bool = True):
        super().__init__()
        self.on_delete_files = on_delete_files
        self.language = language
        # Initial checkbox state for the clean dialog — from Settings, not a
        # hardcoded True.
        self.trash_default = trash_default

        self.selected_directories: List[str] = []
        self.active_mode = "empty_folders"  # "empty_folders", "broken_shortcuts", "junk_files"

        self.empty_folders_list: List[str] = []
        self.broken_shortcuts_list: List[Dict[str, str]] = []
        self.junk_files_list: List[FileInfo] = []

        self.selected_items: Set[str] = set()
        # C2 snapshots {path: (size, mtime)} captured at scan time — the clean
        # worker re-verifies each file before deleting it (round 3).
        self._scan_snapshots: Dict[str, tuple] = {}
        self.cancel_flag = [False]
        self.loaded_count = 0
        # True while a sweep scan or a clean worker is running — blocks
        # re-entrant starts (double-click used to launch two workers).
        self._scan_busy = False

        self.scroll = ft.ScrollMode.AUTO
        self.expand = True
        self.spacing = 14

        # File Picker
        self.pick_dir_dialog = ft.FilePicker()

        # UI components
        self.folders_container = ft.Column(spacing=6)
        self.folders_empty_hint = ft.Container(
            content=ft.Row([
                ft.Icon(ft.Icons.FOLDER_OFF_OUTLINED, color=TEXT_MUTED, size=22),
                ft.Text(get_text("no_directory", self.language), color=TEXT_MUTED, size=13, weight=ft.FontWeight.W_500),
            ], alignment=ft.MainAxisAlignment.CENTER, spacing=10),
            padding=ft.Padding.symmetric(vertical=24, horizontal=16),
            bgcolor=SURFACE_HOVER,
            border=ft.Border.all(1, BORDER_COLOR),
            border_radius=10
        )

        self.progress_bar = ft.ProgressBar(value=None, color=PRIMARY_COLOR, bgcolor=SURFACE_HOVER, visible=False)
        self.status_text = ft.Text("", size=13, color=TEXT_SECONDARY)
        # Hidden while idle: a lone icon above the "Start scan" button read as
        # a stray glyph (the progress card keeps its layout when empty).
        self.status_icon = ft.Icon(ft.Icons.CLEANING_SERVICES_ROUNDED, size=18, color=PRIMARY_COLOR, visible=False)

        self.results_column = ft.Column(spacing=6)
        self.load_more_btn = get_outlined_button(
            text=get_text("load_more", self.language),
            on_click=self.load_more_items,
            icon=ft.Icons.EXPAND_MORE_ROUNDED,
            height=36
        )
        self.load_more_btn.visible = False

        self.clean_button = get_primary_button(
            text=get_text("clean_empty_folders_btn", self.language),
            on_click=self.on_clean_clicked,
            icon=ft.Icons.DELETE_SWEEP_ROUNDED,
            bgcolor=DANGER_COLOR,
            height=42
        )
        self.clean_button.visible = False

        self.scan_button = get_primary_button(
            text=get_text("start_scan", self.language),
            on_click=self.start_sweep_scan,
            icon=ft.Icons.SEARCH_ROUNDED,
            height=44
        )
        # The sweep workers always checked cancel_flag — this button finally
        # gives the UI a way to set it.
        self.cancel_button = get_outlined_button(
            text=get_text("cancel_scan", self.language),
            on_click=self.cancel_sweep_scan,
            icon=ft.Icons.STOP_CIRCLE_OUTLINED,
            height=44
        )
        self.cancel_button.visible = False

        self.build_ui()

    def get_detected_drives(self) -> list[str]:
        return get_detected_drives()

    def add_drive(self, drive: str):
        if drive not in self.selected_directories:
            self.selected_directories.append(drive)
            self.update_folders_list()

    def build_ui(self):
        drive_chips = [
            get_drive_chip(
                drive_label=d,
                on_click=lambda _, drv=d: self.add_drive(drv),
                is_selected=(d in self.selected_directories)
            )
            for d in self.get_detected_drives()
        ]

        mode_options = [
            ("empty_folders", get_text('empty_folders_tab', self.language), ft.Icons.FOLDER_OFF_OUTLINED),
            ("broken_shortcuts", get_text('broken_shortcuts_tab', self.language), ft.Icons.LINK_OFF_ROUNDED),
            ("junk_files", get_text('junk_files_tab', self.language), ft.Icons.DELETE_SWEEP_OUTLINED),
        ]

        self.controls = [
            # Header
            get_header_row(
                title=get_text("sweeper_title", self.language),
                subtitle=get_text("sweeper_desc", self.language),
                action_control=ft.Row([
                    get_primary_button(
                        text=get_text("choose_folder", self.language),
                        on_click=self.pick_directory,
                        icon=ft.Icons.CREATE_NEW_FOLDER_OUTLINED,
                        bgcolor=PRIMARY_COLOR
                    ),
                    get_outlined_button(
                        text=get_text("clear_all_folders", self.language),
                        on_click=self.clear_all_folders,
                        icon=ft.Icons.CLEAR_ALL_ROUNDED,
                    )
                ], spacing=8)
            ),

            # Folders Card
            get_styled_card(
                ft.Column([
                    ft.Row([
                        ft.Icon(ft.Icons.FOLDER_SPECIAL_ROUNDED, color=PRIMARY_COLOR, size=20),
                        ft.Text(get_text("selected_folders", self.language), size=14, weight=ft.FontWeight.BOLD, color=TEXT_PRIMARY),
                        get_badge(f"{len(self.selected_directories)}", color=PRIMARY_COLOR),
                        ft.Container(expand=True),
                        ft.Row([ft.Text(get_text("quick_drive_add", self.language), size=12, color=TEXT_MUTED, weight=ft.FontWeight.W_500)] + drive_chips, spacing=6, wrap=True, vertical_alignment=ft.CrossAxisAlignment.CENTER)
                    ]),
                    self.folders_container if self.selected_directories else self.folders_empty_hint
                ], spacing=10)
            ),

            # Mode Selector (Pill Segmented Control)
            ft.Row([
                get_segmented_control(
                    options=mode_options,
                    selected_key=self.active_mode,
                    on_select=self.set_mode
                )
            ], alignment=ft.MainAxisAlignment.START),

            # Action & Progress Card
            get_progress_card(
                status_icon=self.status_icon,
                status_text=self.status_text,
                progress_bar=self.progress_bar,
                primary_action_btn=ft.Row([self.scan_button, self.cancel_button], spacing=8),
                secondary_action_btn=self.clean_button
            ),

            # Results Section
            self.results_column,
            ft.Row([self.load_more_btn], alignment=ft.MainAxisAlignment.CENTER)
        ]

    async def pick_directory(self, e):
        path = await self.pick_dir_dialog.get_directory_path()
        if path and path not in self.selected_directories:
            self.selected_directories.append(path)
            self.update_folders_list()

    def remove_folder(self, folder):
        if folder in self.selected_directories:
            self.selected_directories.remove(folder)
            self.update_folders_list()

    def clear_all_folders(self, e):
        self.selected_directories.clear()
        self.update_folders_list()

    def update_folders_list(self):
        self.folders_container.controls.clear()
        for folder in self.selected_directories:
            self.folders_container.controls.append(
                get_folder_list_item(
                    path=folder,
                    on_remove=lambda _, f=folder: self.remove_folder(f),
                    badge_color=PRIMARY_COLOR,
                    icon_color=PRIMARY_COLOR,
                    remove_tooltip=get_text("remove", self.language)
                )
            )
        self.build_ui()
        try:
            self.update()
        except Exception:
            pass

    def set_mode(self, mode: str):
        # Switching mode mid-scan would make the worker's finally-block render
        # results for a different mode than it collected (and could even feed
        # folder paths to a file-mode clean). Ignore until the scan finishes.
        if self._scan_busy:
            return
        self.active_mode = mode
        self.clean_button.visible = False
        self.results_column.controls.clear()
        self.load_more_btn.visible = False
        self.build_ui()
        try:
            self.update()
        except Exception:
            pass

    def start_sweep_scan(self, e):
        if not self.selected_directories:
            self.status_text.value = get_text("please_select_dir", self.language)
            self.status_text.color = DANGER_COLOR
            self.update()
            return
        if self._scan_busy:
            return

        self._scan_busy = True
        self.cancel_flag[0] = False
        self.progress_bar.visible = True
        self.status_icon.visible = True
        self.status_text.value = get_text("scanning", self.language)
        self.status_text.color = TEXT_SECONDARY
        self.results_column.controls.clear()
        self.clean_button.visible = False
        self.load_more_btn.visible = False
        self.scan_button.visible = False
        self.cancel_button.visible = True
        self.selected_items.clear()
        self.loaded_count = 0
        self.update()

        def _worker():
            try:
                if self.active_mode == "empty_folders":
                    res = find_empty_directories(self.selected_directories, cancel_flag=self.cancel_flag)
                    self.empty_folders_list = res
                    self.selected_items = set(res)
                    self._scan_snapshots = {}  # rmdir is inherently safe (empty only)
                elif self.active_mode == "broken_shortcuts":
                    res = find_broken_shortcuts(self.selected_directories, cancel_flag=self.cancel_flag)
                    self.broken_shortcuts_list = res
                    self.selected_items = set(r["path"] for r in res)
                    self._scan_snapshots = {r["path"]: (r["size"], r.get("modified", 0.0)) for r in res}
                else:
                    res = find_junk_files(self.selected_directories, cancel_flag=self.cancel_flag)
                    self.junk_files_list = res
                    self._scan_snapshots = {r.path: (r.size, r.modified) for r in res}
                    # Pre-select only unambiguous junk; backups/dumps and large
                    # files stay unchecked (see _JUNK_NO_PRESELECT_EXT).
                    self.selected_items = {
                        r.path for r in res
                        if r.size <= _JUNK_PRESELECT_MAX_BYTES
                        and os.path.splitext(r.path)[1].lower() not in _JUNK_NO_PRESELECT_EXT
                    }
            finally:
                self._scan_busy = False
                self.render_sweep_results()

        self._run_worker(_worker)

    def cancel_sweep_scan(self, e):
        """The scan workers have always checked cancel_flag — this button is
        what finally sets it from the UI."""
        self.cancel_flag[0] = True
        self.status_icon.visible = False
        self.status_text.value = get_text("scan_cancelled", self.language)
        self.status_text.color = TEXT_SECONDARY
        self.cancel_button.visible = False
        try:
            self.update()
        except Exception:
            pass

    def _run_worker(self, worker):
        """Background work must run through page.run_thread (flet page context,
        same class of fix as H3 in main.py). Falls back to a plain thread when
        the view is not attached to a page (headless tests, detached views)."""
        page = self._page_or_none()
        if page is not None and hasattr(page, "run_thread"):
            page.run_thread(worker)
        else:
            threading.Thread(target=worker, daemon=True).start()

    def render_sweep_results(self):
        self.progress_bar.visible = False
        self.status_icon.visible = False
        self.status_text.value = ""
        self.scan_button.visible = True
        self.cancel_button.visible = False
        self.loaded_count = 0
        self.results_column.controls.clear()
        self.load_more_items(None)

    def load_more_items(self, e):
        rows = []
        if self.active_mode == "empty_folders":
            total = len(self.empty_folders_list)
            if total == 0:
                rows.append(ft.Text(get_text("no_empty_folders", self.language), color=TEXT_MUTED))
            else:
                self.clean_button.content = ft.Row([
                    ft.Icon(ft.Icons.DELETE_SWEEP_ROUNDED, size=18, color="#FFFFFF"),
                    ft.Text(f"{get_text('clean_empty_folders_btn', self.language)} ({len(self.selected_items)})", color="#FFFFFF", weight=ft.FontWeight.W_600)
                ], spacing=6, alignment=ft.MainAxisAlignment.CENTER)
                self.clean_button.visible = True

                start = self.loaded_count
                end = min(start + self.ITEMS_PER_PAGE, total)
                for path in self.empty_folders_list[start:end]:
                    cb = ft.Checkbox(value=path in self.selected_items, on_change=lambda e, p=path: self.toggle_item(p, e.control.value))
                    rows.append(
                        ft.Container(
                            content=ft.Row([
                                cb,
                                ft.Icon(ft.Icons.FOLDER_OFF_ROUNDED, color=WARNING_COLOR, size=18),
                                ft.Text(path, size=13, color=TEXT_PRIMARY, expand=True),
                            ]),
                            bgcolor=SURFACE_HOVER,
                            border_radius=8,
                            padding=8
                        )
                    )
                self.loaded_count = end
                self.load_more_btn.visible = self.loaded_count < total

        elif self.active_mode == "broken_shortcuts":
            total = len(self.broken_shortcuts_list)
            if total == 0:
                rows.append(ft.Text(get_text("no_broken_shortcuts", self.language), color=TEXT_MUTED))
            else:
                self.clean_button.content = ft.Row([
                    ft.Icon(ft.Icons.DELETE_SWEEP_ROUNDED, size=18, color="#FFFFFF"),
                    ft.Text(f"{get_text('clean_broken_shortcuts_btn', self.language)} ({len(self.selected_items)})", color="#FFFFFF", weight=ft.FontWeight.W_600)
                ], spacing=6, alignment=ft.MainAxisAlignment.CENTER)
                self.clean_button.visible = True

                start = self.loaded_count
                end = min(start + self.ITEMS_PER_PAGE, total)
                for item in self.broken_shortcuts_list[start:end]:
                    cb = ft.Checkbox(value=item["path"] in self.selected_items, on_change=lambda e, p=item["path"]: self.toggle_item(p, e.control.value))
                    rows.append(
                        ft.Container(
                            content=ft.Row([
                                cb,
                                ft.Icon(ft.Icons.LINK_OFF_ROUNDED, color=DANGER_COLOR, size=18),
                                ft.Column([
                                    ft.Text(item["name"], size=13, weight=ft.FontWeight.BOLD, color=TEXT_PRIMARY),
                                    ft.Text(f"{get_text('target_path', self.language)}: {item['target']}", size=11, color=TEXT_MUTED),
                                ], expand=True, spacing=2)
                            ]),
                            bgcolor=SURFACE_HOVER,
                            border_radius=8,
                            padding=8
                        )
                    )
                self.loaded_count = end
                self.load_more_btn.visible = self.loaded_count < total

        else:
            total = len(self.junk_files_list)
            if total == 0:
                rows.append(ft.Text(get_text("no_junk_files", self.language), color=TEXT_MUTED))
            else:
                total_size = sum(f.size for f in self.junk_files_list if f.path in self.selected_items)
                self.clean_button.content = ft.Row([
                    ft.Icon(ft.Icons.DELETE_SWEEP_ROUNDED, size=18, color="#FFFFFF"),
                    ft.Text(f"{get_text('clean_junk_files_btn', self.language)} ({len(self.selected_items)} • {format_file_size(total_size)})", color="#FFFFFF", weight=ft.FontWeight.W_600)
                ], spacing=6, alignment=ft.MainAxisAlignment.CENTER)
                self.clean_button.visible = True

                start = self.loaded_count
                end = min(start + self.ITEMS_PER_PAGE, total)
                for f in self.junk_files_list[start:end]:
                    cb = ft.Checkbox(value=f.path in self.selected_items, on_change=lambda e, p=f.path: self.toggle_item(p, e.control.value))
                    rows.append(
                        ft.Container(
                            content=ft.Row([
                                cb,
                                ft.Icon(ft.Icons.DELETE_OUTLINE_ROUNDED, color=DANGER_COLOR, size=18),
                                ft.Column([
                                    ft.Text(f.name, size=13, weight=ft.FontWeight.BOLD, color=TEXT_PRIMARY),
                                    ft.Text(f.path, size=11, color=TEXT_MUTED),
                                ], expand=True, spacing=2),
                                get_badge(format_file_size(f.size), color=WARNING_COLOR)
                            ]),
                            bgcolor=SURFACE_HOVER,
                            border_radius=8,
                            padding=8
                        )
                    )
                self.loaded_count = end
                self.load_more_btn.visible = self.loaded_count < total

        if rows:
            self.results_column.controls.append(
                get_styled_card(ft.Column(rows, spacing=8), padding=12)
            )

        try:
            self.update()
        except Exception:
            pass

    def toggle_item(self, path: str, value: bool):
        if value:
            self.selected_items.add(path)
        else:
            self.selected_items.discard(path)

        # Update clean button counter
        if self.active_mode == "empty_folders":
            self.clean_button.content = ft.Row([
                ft.Icon(ft.Icons.DELETE_SWEEP_ROUNDED, size=18, color="#FFFFFF"),
                ft.Text(f"{get_text('clean_empty_folders_btn', self.language)} ({len(self.selected_items)})", color="#FFFFFF", weight=ft.FontWeight.W_600)
            ], spacing=6, alignment=ft.MainAxisAlignment.CENTER)
        elif self.active_mode == "broken_shortcuts":
            self.clean_button.content = ft.Row([
                ft.Icon(ft.Icons.DELETE_SWEEP_ROUNDED, size=18, color="#FFFFFF"),
                ft.Text(f"{get_text('clean_broken_shortcuts_btn', self.language)} ({len(self.selected_items)})", color="#FFFFFF", weight=ft.FontWeight.W_600)
            ], spacing=6, alignment=ft.MainAxisAlignment.CENTER)
        else:
            total_size = sum(f.size for f in self.junk_files_list if f.path in self.selected_items)
            self.clean_button.content = ft.Row([
                ft.Icon(ft.Icons.DELETE_SWEEP_ROUNDED, size=18, color="#FFFFFF"),
                ft.Text(f"{get_text('clean_junk_files_btn', self.language)} ({len(self.selected_items)} • {format_file_size(total_size)})", color="#FFFFFF", weight=ft.FontWeight.W_600)
            ], spacing=6, alignment=ft.MainAxisAlignment.CENTER)

        try:
            self.clean_button.update()
        except Exception:
            pass

    def _page_or_none(self):
        """flet's Control.page raises RuntimeError instead of returning None for
        controls not yet added to the page — this keeps headless use safe."""
        try:
            return self.page
        except RuntimeError:
            return None

    def on_clean_clicked(self, e):
        if not self.selected_items:
            return
        page = self._page_or_none()
        if page is None:
            return

        total = len(self.selected_items)

        def cancel_dialog(_):
            page.pop_dialog()

        def confirm_clean(use_trash: bool):
            page.pop_dialog()
            # The worker gets the page captured here — see execute_clean.
            self.execute_clean(use_trash, page=page)

        # H4: no destructive action without an explicit confirmation dialog.
        # Empty folders are removed with os.rmdir (which can only ever remove a
        # truly empty directory); the Recycle Bin option applies to file modes.
        show_trash_option = self.active_mode != "empty_folders" and HAS_SEND2TRASH
        trash_checkbox = ft.Checkbox(label=get_text("send_to_trash_label", self.language), value=self.trash_default)

        content_controls = [
            ft.Text(get_text("sweep_delete_confirm", self.language).format(total), size=14)
        ]
        if show_trash_option:
            content_controls.append(trash_checkbox)

        dlg = get_styled_dialog(
            title=get_text("confirm_deletion_title", self.language),
            icon=ft.Icons.DELETE_FOREVER_ROUNDED,
            icon_color=DANGER_COLOR,
            content=ft.Column(content_controls, tight=True, spacing=12),
            actions=[
                get_outlined_button(text=get_text("cancel", self.language), on_click=cancel_dialog),
                get_primary_button(
                    text=get_text("delete", self.language),
                    on_click=lambda _: confirm_clean(
                        use_trash=trash_checkbox.value if show_trash_option else False
                    ),
                    bgcolor=DANGER_COLOR,
                    icon=ft.Icons.DELETE_ROUNDED
                )
            ]
        )
        page.show_dialog(dlg)

    def execute_clean(self, use_trash: bool, page=None):
        """page is captured at confirmation time: if the user switches tabs
        mid-clean the view gets recreated, and a dialog shown through the NEW
        page reference (or a detached self) would simply never appear."""
        total = len(self.selected_items)
        self._scan_busy = True
        self.progress_bar.visible = True
        self.progress_bar.value = 0
        self.status_text.value = get_text("deleting", self.language).format(0, total)
        self.status_text.color = TEXT_SECONDARY
        self.clean_button.visible = False
        self.scan_button.visible = False
        try:
            self.update()
        except Exception:
            pass

        state = {"deleted_count": 0, "freed": 0, "errors": [], "deleted_paths": []}

        def _worker():
            target_page = page if page is not None else self._page_or_none()
            try:
                CHUNK = 200
                items = list(self.selected_items)

                if self.active_mode == "empty_folders":
                    deleted_count, errs = delete_empty_directories(items)
                    state["deleted_count"] = deleted_count
                    state["errors"].extend(errs)
                    deleted = {f for f in items if not os.path.exists(f)}
                    state["deleted_paths"] = sorted(deleted)
                    # Failed folders stay listed so the user can retry them.
                    self.empty_folders_list = [f for f in self.empty_folders_list if f not in deleted]
                else:
                    for start in range(0, len(items), CHUNK):
                        chunk = items[start:start + CHUNK]
                        for path in chunk:
                            # C2: only delete the file that was actually scanned —
                            # a junk path reused by a real file since the scan
                            # (e.g. an app started writing a fresh .log) is skipped.
                            snap = self._scan_snapshots.get(path)
                            if snap:
                                ok, reason = verify_file_unchanged(path, snap[0], snap[1])
                                if not ok:
                                    state["errors"].append(get_text("error_verify_failed", self.language).format(path, reason))
                                    continue
                            try:
                                stat = os.stat(path)
                            except FileNotFoundError:
                                continue  # already gone since the scan
                            except OSError as ex:
                                state["errors"].append(f"{os.path.basename(path)}: {ex}")
                                continue
                            try:
                                if use_trash:
                                    if not HAS_SEND2TRASH:
                                        # Never silently delete forever when the
                                        # user asked for the Recycle Bin (H5/A4).
                                        state["errors"].append(get_text("trash_unavailable", self.language).format(path))
                                        continue
                                    try:
                                        send2trash(path)
                                    except Exception as trash_ex:
                                        # H5: a failed move to the Recycle Bin must
                                        # never become an unrecoverable delete.
                                        state["errors"].append(get_text("trash_failed", self.language).format(path, trash_ex))
                                        continue
                                else:
                                    os.remove(path)
                                state["deleted_count"] += 1
                                state["freed"] += stat.st_size
                                state["deleted_paths"].append(path)
                            except Exception as ex:
                                state["errors"].append(f"{os.path.basename(path)}: {ex}")

                        # Update progress
                        done = min(start + CHUNK, len(items))
                        self.progress_bar.value = done / len(items) if items else 1
                        self.status_text.value = get_text("deleting", self.language).format(state["deleted_count"], total)
                        try:
                            self.update()
                        except Exception:
                            pass

                    deleted = set(state["deleted_paths"])
                    if self.active_mode == "broken_shortcuts":
                        self.broken_shortcuts_list = [s for s in self.broken_shortcuts_list if s["path"] not in deleted]
                    else:
                        self.junk_files_list = [j for j in self.junk_files_list if j.path not in deleted]

                self.selected_items.clear()

                # Journal the removal (like ResultsView deletions); a journal
                # failure is reported, never swallowed.
                if state["deleted_paths"]:
                    try:
                        log_delete_operation(
                            state["deleted_paths"], state["freed"],
                            use_trash=(use_trash and self.active_mode != "empty_folders")
                        )
                    except Exception as log_ex:
                        state["errors"].append(f"operations log: {log_ex}")

                # Hide progress
                self.progress_bar.visible = False
                self.status_text.value = ""
                self.render_sweep_results()

                # Show feedback dialog
                if target_page:
                    msg = get_text("sweeper_clean_complete", self.language).format(state["deleted_count"])
                    if state["freed"]:
                        msg += "\n" + get_text("deleted_space_freed", self.language).format(format_file_size(state["freed"]))
                    if state["errors"]:
                        msg += f"\n\n{get_text('sweeper_clean_errors', self.language).format(len(state['errors']))}"
                        msg += "\n" + "\n".join(state["errors"][:5])
                    dlg = get_styled_dialog(
                        title=get_text("deletion_complete", self.language),
                        title_color=SUCCESS_COLOR,
                        icon=ft.Icons.CHECK_CIRCLE_ROUNDED,
                        icon_color=SUCCESS_COLOR,
                        content=ft.Text(msg, color=TEXT_SECONDARY),
                        actions=[get_primary_button(
                            text=get_text("ok", self.language),
                            on_click=lambda _: target_page.pop_dialog(),
                            bgcolor=PRIMARY_COLOR
                        )]
                    )
                    target_page.show_dialog(dlg)
                    try:
                        target_page.update()
                    except Exception:
                        pass
            finally:
                self._scan_busy = False

        self._run_worker(_worker)
