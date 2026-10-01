import flet as ft
import os
import csv
import html
import json
import datetime
import functools
import threading
from typing import Dict, List, Optional, Set, Tuple
from PIL import Image

from scanner import FileInfo, is_system_path, format_file_size, compute_wasted_bytes
from ui.components import (
    get_styled_card, get_primary_button, get_outlined_button, get_header_row, get_badge,
    get_kpi_badge, format_path_short, get_current_theme, get_styled_dialog, get_action_icon_button,
    PRIMARY_COLOR, SUCCESS_COLOR, DANGER_COLOR, INFO_COLOR, WARNING_COLOR,
    TEXT_PRIMARY, TEXT_SECONDARY, TEXT_MUTED, BORDER_COLOR, SURFACE_HOVER,
    CATEGORY_ICONS
)
from locales import get_text
from app_logging import get_logger
from app_info import APP_VERSION
from ui.thumbnails import get_cached_thumbnail
from folder_priorities import is_priority_path, load_priority_folders, save_priority_folders

logger = get_logger(__name__)

class ResultsView(ft.Column):
    GROUPS_PER_PAGE = 50
    # 2.1c: keystroke debounce — rebuilding/redrawing the whole column is
    # O(groups), so the refresh waits for a pause in typing.
    SEARCH_DEBOUNCE_DELAY = 0.25
    # 2.1d: the lightbox opens on the cached thumbnail and swaps the
    # full-size file in after this beat (lets the client paint the preview
    # before the original starts streaming).
    LIGHTBOX_FULL_IMAGE_DELAY = 0.15

    def __init__(self, results: Dict[str, List[FileInfo]], on_back, on_delete, on_hardlink=None, on_move=None, language="ru", allow_hardlink: bool = True, trash_default: bool = True, content_verified: bool = True, trash_available: bool = True):
        super().__init__()
        self.all_results = dict(results)
        self.filtered_results = dict(results)
        self.on_back = on_back
        self.on_delete = on_delete
        self.on_hardlink = on_hardlink
        # "Move to folder" — the reversible alternative to deletion.
        self.on_move = on_move
        self.language = language
        self.allow_hardlink = allow_hardlink
        # False when send2trash is unavailable: the "to Recycle Bin" checkbox is
        # then disabled and unchecked, so confirming the dialog explicitly means
        # permanent deletion instead of silently falling back to it (round 3 A4).
        self.trash_available = trash_available
        # False when files were grouped by size/name only (content never
        # compared) — such groups must NOT be preselected for deletion, and a
        # warning banner is shown (round 3: a size-only scan preselected
        # content-different files for one-click deletion).
        self.content_verified = content_verified
        # Initial checkbox state for the delete dialog — comes from Settings
        # ("move to Recycle Bin by default") instead of a hardcoded True.
        self.trash_default = trash_default
        # True while a delete/hardlink worker is running for this view — the
        # action buttons are no-ops until it finishes (a second click used to
        # launch a second worker over the same selection).
        self._operation_busy = False

        # Global selection state
        self.selected_paths: Set[str] = set()

        self.active_category: str = "all"
        self.search_query: str = ""
        self.loaded_groups_count: int = 0
        # Pending debounce timer for the search field (2.1c).
        self._search_debounce_timer: Optional[threading.Timer] = None

        # Filter / sort / view state
        self.min_size_filter: int = 0
        self.date_filter: str = "any"
        self.sort_mode: str = "wasted"
        self.grid_mode: bool = False
        self.collapsed_groups: Set[str] = set()

        self.scroll = ft.ScrollMode.AUTO
        self.expand = True
        self.spacing = 14

        # FilePicker for export
        self.export_picker = ft.FilePicker()
        self.pending_export_type = "csv"
        # FilePicker for the "move to folder" destination.
        self.move_picker = ft.FilePicker()

        # Calculate Overall Stats
        self.total_groups = len(results)
        self.total_dupe_files = sum(max(0, len(files) - 1) for files in results.values())
        self.total_wasted_bytes = compute_wasted_bytes(results)

        # Scan-time (path, size, mtime) snapshots; the delete/hardlink worker
        # re-verifies each file against these before touching it (C2 TOCTOU).
        self._path_to_info: Dict[str, FileInfo] = {
            f.path: f for files in results.values() for f in files
        }

        # Count and Size by Categories
        self.category_counts = {"all": self.total_groups}
        self.category_wasted = {}
        for files in results.values():
            if files:
                cat = files[0].category
                self.category_counts[cat] = self.category_counts.get(cat, 0) + 1
                wasted = sum(f.size for f in files[1:])
                self.category_wasted[cat] = self.category_wasted.get(cat, 0) + wasted

        # UI Components
        self.search_field = ft.TextField(
            hint_text=get_text("search_placeholder", self.language),
            prefix_icon=ft.Icons.SEARCH_ROUNDED,
            bgcolor=SURFACE_HOVER,
            border_color=BORDER_COLOR,
            text_size=13,
            height=40,
            expand=True,
            on_change=self.on_search_change
        )

        self.size_filter_dropdown = ft.Dropdown(
            label=get_text("flt_size_label", self.language),
            width=160,
            text_size=12,
            border_color=BORDER_COLOR,
            bgcolor=SURFACE_HOVER,
            value="0",
            options=[
                ft.dropdown.Option("0", get_text("flt_size_any", self.language)),
                ft.dropdown.Option(str(1024 ** 2), get_text("flt_size_gt1", self.language)),
                ft.dropdown.Option(str(10 * 1024 ** 2), get_text("flt_size_gt10", self.language)),
                ft.dropdown.Option(str(100 * 1024 ** 2), get_text("flt_size_gt100", self.language)),
                ft.dropdown.Option(str(1024 ** 3), get_text("flt_size_gt1gb", self.language)),
            ],
            on_select=self.on_filter_changed
        )

        self.date_filter_dropdown = ft.Dropdown(
            label=get_text("flt_date_label", self.language),
            width=180,
            text_size=12,
            border_color=BORDER_COLOR,
            bgcolor=SURFACE_HOVER,
            value="any",
            options=[
                ft.dropdown.Option("any", get_text("flt_date_any", self.language)),
                ft.dropdown.Option("year", get_text("flt_date_year", self.language)),
                ft.dropdown.Option("older", get_text("flt_date_older", self.language)),
            ],
            on_select=self.on_filter_changed
        )

        self.sort_dropdown = ft.Dropdown(
            label=get_text("flt_sort_label", self.language),
            width=200,
            text_size=12,
            border_color=BORDER_COLOR,
            bgcolor=SURFACE_HOVER,
            value="wasted",
            options=[
                ft.dropdown.Option("wasted", get_text("flt_sort_wasted", self.language)),
                ft.dropdown.Option("count", get_text("flt_sort_count", self.language)),
                ft.dropdown.Option("size", get_text("flt_sort_size", self.language)),
            ],
            on_select=self.on_filter_changed
        )

        self.grid_toggle_btn = get_action_icon_button(
            icon=ft.Icons.GRID_VIEW_ROUNDED if not self.grid_mode else ft.Icons.VIEW_LIST_ROUNDED,
            icon_color=TEXT_MUTED,
            tooltip=get_text("view_grid" if not self.grid_mode else "view_list", self.language),
            on_click=self.on_grid_toggle,
            icon_size=20,
            button_size=34
        )

        self.smart_select_dropdown = ft.Dropdown(
            label=get_text("smart_select", self.language),
            width=240,
            text_size=12,
            border_color=BORDER_COLOR,
            bgcolor=SURFACE_HOVER,
            options=[
                ft.dropdown.Option("first", get_text("select_all_except_first", self.language)),
                ft.dropdown.Option("newest", get_text("keep_newest", self.language)),
                ft.dropdown.Option("oldest", get_text("keep_oldest", self.language)),
                ft.dropdown.Option("largest_res", get_text("keep_largest_res", self.language)),
                ft.dropdown.Option("priority", get_text("select_except_priority", self.language)),
                ft.dropdown.Option("last", get_text("select_all_except_last", self.language)),
                ft.dropdown.Option("shortest", get_text("select_shortest_path", self.language)),
                ft.dropdown.Option("all", get_text("select_all", self.language)),
                ft.dropdown.Option("none", get_text("deselect_all", self.language)),
                ft.dropdown.Option("invert", get_text("invert_selection", self.language)),
            ],
            on_select=self.apply_smart_selection
        )

        self.priority_folders_btn = get_action_icon_button(
            icon=ft.Icons.FOLDER_SPECIAL_OUTLINED,
            tooltip=get_text("folder_priorities", self.language),
            on_click=self.show_priority_folders_dialog
        )

        self.move_btn = get_action_icon_button(
            icon=ft.Icons.DRIVE_FILE_MOVE_ROUNDED,
            tooltip=get_text("move_to_folder", self.language),
            on_click=self.on_move_clicked
        )
        # No handler wired (headless tests) — hide instead of dead-clicking.
        self.move_btn.visible = self.on_move is not None

        self.delete_btn = get_primary_button(
            text=f"{get_text('delete_selected', self.language)} (0)",
            on_click=self.on_delete_clicked,
            icon=ft.Icons.DELETE_SWEEP_ROUNDED,
            bgcolor=DANGER_COLOR,
            height=40
        )

        self.hardlink_btn = get_primary_button(
            text=get_text("hardlink_selected", self.language),
            on_click=self.on_hardlink_clicked,
            icon=ft.Icons.LINK_ROUNDED,
            bgcolor=PRIMARY_COLOR,
            height=40
        )
        # C1a: hardlinks require byte-identical files — never offer the button
        # for "visually similar" photo clusters.
        self.hardlink_btn.visible = self.allow_hardlink

        self.category_chips_row = ft.Row(spacing=6, scroll=ft.ScrollMode.AUTO)
        self.results_column = ft.Column(spacing=10)

        self.load_more_btn = get_outlined_button(
            text=get_text("load_more", self.language),
            on_click=self.load_more_groups,
            icon=ft.Icons.EXPAND_MORE_ROUNDED,
            height=38
        )
        self.progress_counter_text = ft.Text("", size=12, color=TEXT_MUTED)
        self.operation_progress = ft.ProgressBar(value=0, color=PRIMARY_COLOR, bgcolor=SURFACE_HOVER, visible=False)
        self.operation_status = ft.Text("", size=12, color=TEXT_SECONDARY, visible=False)

        # Pre-select duplicates for deletion ONLY when the content was actually
        # verified identical (hash/byte compare). pHash results are visually
        # similar (not identical), and size/name-only scans never compared
        # content at all — pre-selecting either invites a one-click loss of
        # files that are not true duplicates (round 3).
        if self.allow_hardlink and self.content_verified:
            self.select_default_duplicates()
        self.build_ui()
        self.refresh_filtered_results()

    def select_default_duplicates(self):
        self.selected_paths.clear()
        for files in self.all_results.values():
            # Never pre-select 0-byte "duplicates": hardlinking/deleting them
            # frees nothing, and they are often deliberate markers (__init.py,
            # .gitkeep, lock files) whose mass removal breaks projects (round 3).
            if not files or files[0].size == 0:
                continue
            for i, f in enumerate(files):
                if i > 0:
                    self.selected_paths.add(f.path)

    def build_master_kpi_strip(self) -> ft.Container:
        """Compact horizontal master KPI status strip."""
        selected_size = sum(
            f.size for files in self.all_results.values() for f in files if f.path in self.selected_paths
        )

        kpis = [
            get_kpi_badge(
                ft.Icons.DELETE_SWEEP_ROUNDED,
                get_text("similar_total_size" if not self.allow_hardlink else "wasted_space", self.language),
                format_file_size(self.total_wasted_bytes),
                color=DANGER_COLOR
            ),
            get_kpi_badge(ft.Icons.FOLDER_ZIP_OUTLINED, get_text("duplicate_groups", self.language), str(self.total_groups), color=PRIMARY_COLOR),
            get_kpi_badge(ft.Icons.CONTENT_COPY_ROUNDED, get_text("duplicate_files_count", self.language), str(self.total_dupe_files), color=INFO_COLOR),
            get_kpi_badge(ft.Icons.CHECK_CIRCLE_OUTLINE_ROUNDED, f"{get_text('kpi_selected', self.language)}:", f"{len(self.selected_paths)} ({format_file_size(selected_size)})", color=SUCCESS_COLOR),
        ]

        # Compact category distribution dots
        cat_dots = []
        if self.total_wasted_bytes > 0:
            for cat, wasted in self.category_wasted.items():
                if wasted > 0:
                    pct = (wasted / self.total_wasted_bytes) * 100
                    _, cat_color = CATEGORY_ICONS.get(cat, CATEGORY_ICONS["other"])
                    cat_dots.append(
                        ft.Row([
                            ft.Container(width=8, height=8, bgcolor=cat_color, border_radius=2),
                            ft.Text(f"{get_text(f'filter_{cat}', self.language)} {pct:.0f}%", size=10, color=TEXT_MUTED)
                        ], spacing=4)
                    )

        return get_styled_card(
            ft.Row([
                ft.Row(kpis, spacing=8, wrap=True, expand=True),
                ft.Row(cat_dots, spacing=10, wrap=True) if cat_dots else ft.Container()
            ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN, vertical_alignment=ft.CrossAxisAlignment.CENTER),
            padding=10
        )

    def build_ui(self):
        theme = get_current_theme()
        # Warning banner for size/name-only scans (content never compared).
        self.unverified_banner = ft.Container(
            content=ft.Row([
                ft.Icon(ft.Icons.WARNING_AMBER_ROUNDED, color=WARNING_COLOR, size=18),
                ft.Text(get_text("unverified_content_warning", self.language), size=12, color=WARNING_COLOR, expand=True)
            ], spacing=8),
            bgcolor=f"{WARNING_COLOR}18",
            border=ft.Border.all(1, f"{WARNING_COLOR}55"),
            border_radius=8,
            padding=10,
            visible=not self.content_verified
        )
        chips = []
        categories = ["all", "images", "videos", "audio", "documents", "archives", "code", "other"]
        for cat in categories:
            count = self.category_counts.get(cat, 0)
            if cat != "all" and count == 0:
                continue
            is_active = (cat == self.active_category)
            chips.append(
                ft.Container(
                    content=ft.Row([
                        ft.Text(get_text(f"filter_{cat}", self.language), size=12, color="#FFFFFF" if is_active else theme["TEXT_SECONDARY"], weight=ft.FontWeight.BOLD if is_active else ft.FontWeight.NORMAL),
                        ft.Container(
                            content=ft.Text(str(count), size=10, color="#FFFFFF" if is_active else theme["TEXT_MUTED"]),
                            bgcolor=f"{theme['PRIMARY_LIGHT']}44" if is_active else theme["SURFACE_HOVER"],
                            border_radius=10,
                            padding=ft.Padding.symmetric(horizontal=6, vertical=1)
                        )
                    ], spacing=6),
                    bgcolor=theme["PRIMARY_COLOR"] if is_active else theme["SURFACE_CARD"],
                    border=ft.Border.all(1, theme["PRIMARY_COLOR"] if is_active else theme["BORDER_COLOR"]),
                    border_radius=16,
                    padding=ft.Padding.symmetric(horizontal=12, vertical=6),
                    on_click=lambda _, c=cat: self.on_category_selected(c)
                )
            )
        self.category_chips_row.controls = chips

        self.controls = [
            # Header Row
            get_header_row(
                title=get_text("scan_results", self.language),
                subtitle=f"{self.total_groups} {get_text('duplicate_groups', self.language)}",
                action_control=ft.Row([
                    get_outlined_button(
                        text=get_text("back_to_search", self.language),
                        on_click=lambda _: self.on_back(),
                        icon=ft.Icons.ARROW_BACK_ROUNDED,
                        height=38
                    ),
                    ft.PopupMenuButton(
                        icon=ft.Icons.DOWNLOAD_ROUNDED,
                        tooltip=get_text("export_report", self.language),
                        items=[
                            ft.PopupMenuItem(content=ft.Text(get_text("export_csv", self.language)), on_click=functools.partial(self.trigger_export, "csv")),
                            ft.PopupMenuItem(content=ft.Text(get_text("export_json", self.language)), on_click=functools.partial(self.trigger_export, "json")),
                            ft.PopupMenuItem(content=ft.Text(get_text("export_txt", self.language)), on_click=functools.partial(self.trigger_export, "txt")),
                            ft.PopupMenuItem(content=ft.Text(get_text("export_html", self.language)), on_click=functools.partial(self.trigger_export, "html")),
                        ]
                    )
                ], spacing=8)
            ),

            # Warning banner: shown when files were grouped by size/name only
            # and their content was never compared (round 3).
            self.unverified_banner,

            # Master KPI Status Strip
            self.build_master_kpi_strip(),

            # Command Deck: Search, Filters, Smart Select & Primary Action Buttons
            get_styled_card(
                ft.Column([
                    ft.Row([
                        self.search_field,
                        self.smart_select_dropdown,
                        self.priority_folders_btn,
                        self.move_btn,
                        self.hardlink_btn,
                        self.delete_btn
                    ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN, wrap=True, spacing=8),
                    self.category_chips_row,
                    ft.Row([
                        self.size_filter_dropdown,
                        self.date_filter_dropdown,
                        self.sort_dropdown,
                        self.grid_toggle_btn
                    ], spacing=10, wrap=True, vertical_alignment=ft.CrossAxisAlignment.CENTER)
                ], spacing=10)
            ),

            # Group Cards Column
            self.results_column,

            # Operation Progress (delete/hardlink)
            ft.Container(
                content=ft.Column([
                    self.operation_progress,
                    self.operation_status
                ], spacing=4, horizontal_alignment=ft.CrossAxisAlignment.CENTER),
                visible=True, padding=0
            ),

            # Load More / Progress Row
            ft.Row([
                self.progress_counter_text,
                self.load_more_btn
            ], alignment=ft.MainAxisAlignment.CENTER, spacing=16)
        ]

    def on_grid_toggle(self, e):
        self.grid_mode = not self.grid_mode
        self.grid_toggle_btn.content.icon = ft.Icons.VIEW_LIST_ROUNDED if self.grid_mode else ft.Icons.GRID_VIEW_ROUNDED
        self.grid_toggle_btn.tooltip = get_text("view_list" if self.grid_mode else "view_grid", self.language)
        self.results_column.controls.clear()
        self.loaded_groups_count = 0
        self.load_more_groups(None)
        self.update()

    def on_filter_changed(self, e):
        try:
            self.min_size_filter = int(self.size_filter_dropdown.value or 0)
        except ValueError:
            self.min_size_filter = 0
        self.date_filter = self.date_filter_dropdown.value or "any"
        self.sort_mode = self.sort_dropdown.value or "wasted"
        self.refresh_filtered_results()

    def on_category_selected(self, category: str):
        self.active_category = category
        self.build_ui()
        self.refresh_filtered_results()

    def on_search_change(self, e):
        # 2.1c: defer the (expensive, whole-column) refresh ~250 ms after the
        # LAST keystroke — a newer keystroke cancels the pending refresh, so
        # fast typing cannot trigger a render storm.
        if self._search_debounce_timer is not None:
            self._search_debounce_timer.cancel()
        timer = threading.Timer(self.SEARCH_DEBOUNCE_DELAY, self._apply_search_query)
        timer.daemon = True
        self._search_debounce_timer = timer
        timer.start()

    def _apply_search_query(self):
        # Only the timer that is CURRENTLY scheduled may run the refresh: a
        # callback racing a newer keystroke (fired just before cancel())
        # must stay silent — the newer timer owns the field now.
        if self._search_debounce_timer is not threading.current_thread():
            return
        self._search_debounce_timer = None
        self.search_query = self.search_field.value.lower().strip()
        self.refresh_filtered_results()

    def refresh_filtered_results(self):
        now = datetime.datetime.now().timestamp()
        filtered = {}

        for key, files in self.all_results.items():
            if not files:
                continue

            if self.active_category != "all":
                if files[0].category != self.active_category:
                    continue

            if self.search_query:
                matches_search = any(
                    self.search_query in f.name.lower() or self.search_query in f.path.lower()
                    for f in files
                )
                if not matches_search:
                    continue

            if self.min_size_filter > 0 and files[0].size < self.min_size_filter:
                continue

            if self.date_filter != "any":
                newest = max(f.modified for f in files)
                one_year_ago = now - 365 * 86400
                if self.date_filter == "year" and newest < one_year_ago:
                    continue
                elif self.date_filter == "older" and newest >= one_year_ago:
                    continue

            filtered[key] = files

        # Sort groups
        items = list(filtered.items())
        if self.sort_mode == "wasted":
            items.sort(key=lambda kv: sum(f.size for f in kv[1][1:]), reverse=True)
        elif self.sort_mode == "count":
            items.sort(key=lambda kv: len(kv[1]), reverse=True)
        elif self.sort_mode == "size":
            items.sort(key=lambda kv: kv[1][0].size if kv[1] else 0, reverse=True)

        self.filtered_results = dict(items)
        self.loaded_groups_count = 0
        self.results_column.controls.clear()
        self.load_more_groups(None)
        if not self.filtered_results and self.all_results:
            # Filters hid everything — an empty column reads as "no results",
            # which is a different (and misleading) statement.
            self.results_column.controls.append(
                ft.Container(
                    content=ft.Text(get_text("no_match_filters", self.language), color=TEXT_MUTED, size=13),
                    padding=20
                )
            )
        self.update_action_button_texts()
        if self.parent:
            try:
                self.update()
            except Exception:
                pass

    def load_more_groups(self, e):
        items = list(self.filtered_results.items())
        start = self.loaded_groups_count
        end = min(start + self.GROUPS_PER_PAGE, len(items))

        for key, files in items[start:end]:
            self.results_column.controls.append(self.build_group_card(key, files))

        self.loaded_groups_count = end
        total_filtered = len(items)

        self.progress_counter_text.value = get_text("showing_groups", self.language).format(self.loaded_groups_count, total_filtered)
        self.load_more_btn.visible = self.loaded_groups_count < total_filtered

        if e and self.parent:
            try:
                self.update()
            except Exception:
                pass

    def toggle_group_collapse(self, key: str):
        if key in self.collapsed_groups:
            self.collapsed_groups.remove(key)
        else:
            self.collapsed_groups.add(key)
        self.results_column.controls.clear()
        items = list(self.filtered_results.items())[:self.loaded_groups_count]
        for k, files in items:
            self.results_column.controls.append(self.build_group_card(k, files))
        self.update()

    def build_group_card(self, key: str, files: List[FileInfo]) -> ft.Container:
        cat = files[0].category if files else "other"
        cat_icon, cat_color = CATEGORY_ICONS.get(cat, CATEGORY_ICONS["other"])
        file_size_str = format_file_size(files[0].size) if files else "0 B"
        wasted_for_group = format_file_size(sum(f.size for f in files[1:])) if files else "0 B"

        is_collapsed = key in self.collapsed_groups

        header_actions = [
            ft.Icon(cat_icon, color=cat_color, size=20),
            ft.Text(files[0].name if files else key, size=14, weight=ft.FontWeight.BOLD, color=TEXT_PRIMARY, expand=True),
            get_badge(f"{len(files)} {get_text('files', self.language)}", color=PRIMARY_COLOR),
            get_badge(f"{file_size_str}", color=INFO_COLOR),
            get_badge(f"-{wasted_for_group}", color=DANGER_COLOR),
        ]

        if cat == "images" and len(files) >= 2:
            header_actions.append(
                get_action_icon_button(
                    icon=ft.Icons.COMPARE_ROUNDED,
                    icon_color=PRIMARY_COLOR,
                    tooltip=get_text("compare_photos_btn", self.language),
                    on_click=lambda _, f=files: self.show_side_by_side_comparison(f)
                )
            )

        header_actions.append(
            get_action_icon_button(
                icon=ft.Icons.EXPAND_LESS_ROUNDED if not is_collapsed else ft.Icons.EXPAND_MORE_ROUNDED,
                icon_color=TEXT_MUTED,
                tooltip=get_text("collapse_tooltip", self.language),
                on_click=lambda _, k=key: self.toggle_group_collapse(k)
            )
        )

        content_list = [
            ft.Row(header_actions, alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
            ft.Divider(color=BORDER_COLOR, height=1),
        ]

        if not is_collapsed:
            if self.grid_mode and cat == "images":
                content_list.append(self.build_image_grid(files))
            else:
                for i, file in enumerate(files):
                    is_sys = is_system_path(file.path)
                    is_selected = file.path in self.selected_paths
                    content_list.append(self.build_file_row(file, i, is_sys, is_selected, files))

        return get_styled_card(
            ft.Column(content_list, spacing=8),
            padding=12
        )

    def build_file_row(self, file: FileInfo, index: int, is_sys: bool, is_selected: bool, group_files: List[FileInfo]) -> ft.Container:
        cb = ft.Checkbox(
            value=is_selected,
            on_change=lambda e, p=file.path: self.on_checkbox_toggle(p, e.control.value)
        )

        thumbnail_widget = None
        if file.category == "images" and os.path.exists(file.path):
            try:
                thumbnail_widget = ft.Container(
                    content=ft.Image(
                        # Cached 160px JPEG instead of the full-size file —
                        # flet ships the whole src to the client otherwise.
                        src=get_cached_thumbnail(file.path, file.modified),
                        width=38,
                        height=38,
                        fit=ft.BoxFit.COVER,
                        border_radius=6,
                        error_content=ft.Icon(ft.Icons.BROKEN_IMAGE_ROUNDED, size=20, color=TEXT_MUTED)
                    ),
                    border_radius=6,
                    border=ft.Border.all(1, BORDER_COLOR),
                    on_click=lambda _, p=file.path: self.show_image_lightbox(p)
                )
            except Exception:
                thumbnail_widget = None

        mod_date_str = datetime.datetime.fromtimestamp(file.modified).strftime("%Y-%m-%d %H:%M")
        drive_letter = os.path.splitdrive(file.path)[0]
        short_p = format_path_short(file.path, max_chars=65)

        badge_status = get_badge(get_text("original_first", self.language), color=SUCCESS_COLOR) if index == 0 else get_badge(get_text("duplicate_label", self.language), color=DANGER_COLOR)

        info_col = ft.Column([
            ft.Row([
                get_badge(drive_letter, color=PRIMARY_COLOR) if drive_letter else ft.Container(),
                ft.Text(short_p, size=13, color=DANGER_COLOR if is_sys else TEXT_PRIMARY, weight=ft.FontWeight.W_500, expand=True, tooltip=file.path),
                badge_status,
                get_badge(get_text("system_file", self.language), color=DANGER_COLOR, icon=ft.Icons.SECURITY_ROUNDED) if is_sys else ft.Container()
            ]),
            ft.Row([
                ft.Text(f"{get_text('modified', self.language)}: {mod_date_str}", size=11, color=TEXT_MUTED),
                ft.Text(f"• {format_file_size(file.size)}", size=11, color=TEXT_MUTED),
            ], spacing=6)
        ], spacing=2, expand=True)

        action_buttons = ft.Row([
            get_action_icon_button(
                icon=ft.Icons.FOLDER_OPEN_ROUNDED,
                tooltip=get_text("open_folder", self.language),
                on_click=lambda _, p=file.path: self.open_in_explorer(p)
            ),
            get_action_icon_button(
                icon=ft.Icons.OPEN_IN_NEW_ROUNDED,
                tooltip=get_text("open_file", self.language),
                on_click=lambda _, p=file.path: self.open_file_natively(p)
            ),
        ], spacing=4)

        row_content = [cb]
        if thumbnail_widget:
            row_content.append(thumbnail_widget)
        row_content.extend([info_col, action_buttons])

        return ft.Container(
            content=ft.Row(row_content, vertical_alignment=ft.CrossAxisAlignment.CENTER),
            bgcolor=f"{PRIMARY_COLOR}12" if is_selected else (SURFACE_HOVER if index % 2 == 0 else "transparent"),
            border=ft.Border.all(1, f"{PRIMARY_COLOR}35" if is_selected else "transparent"),
            border_radius=8,
            padding=ft.Padding.symmetric(horizontal=10, vertical=6)
        )

    def build_image_grid(self, files: List[FileInfo]) -> ft.Row:
        tiles = []
        for i, file in enumerate(files):
            is_selected = file.path in self.selected_paths
            is_sys = is_system_path(file.path)
            tiles.append(
                ft.Container(
                    content=ft.Column([
                        ft.Stack([
                            ft.Image(
                                # Cached thumbnail (see build_file_row).
                                src=get_cached_thumbnail(file.path, file.modified),
                                width=150,
                                height=100,
                                fit=ft.BoxFit.COVER,
                                border_radius=6,
                                error_content=ft.Icon(ft.Icons.BROKEN_IMAGE_ROUNDED, size=24, color=TEXT_MUTED)
                            ),
                            ft.Container(
                                content=ft.Checkbox(
                                    value=is_selected,
                                    on_change=lambda e, p=file.path: self.on_checkbox_toggle(p, e.control.value)
                                ),
                                top=4,
                                left=4
                            ),
                            ft.Container(
                                content=get_badge(get_text("original_first", self.language) if i == 0 else get_text("duplicate_label", self.language), color=SUCCESS_COLOR if i == 0 else DANGER_COLOR),
                                top=4,
                                right=4
                            )
                        ]),
                        ft.Text(os.path.basename(file.path), size=11, weight=ft.FontWeight.BOLD, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                        ft.Text(format_file_size(file.size), size=10, color=TEXT_MUTED),
                    ], spacing=3, horizontal_alignment=ft.CrossAxisAlignment.CENTER),
                    bgcolor=SURFACE_HOVER,
                    border=ft.Border.all(1, DANGER_COLOR if is_sys else (PRIMARY_COLOR if is_selected else BORDER_COLOR)),
                    border_radius=8,
                    padding=6,
                    on_click=lambda _, p=file.path: self.show_image_lightbox(p)
                )
            )
        return ft.Row(tiles, wrap=True, spacing=8)

    def on_checkbox_toggle(self, path: str, value: bool):
        if value:
            self.selected_paths.add(path)
        else:
            self.selected_paths.discard(path)
        self.update_action_button_texts()

    def update_action_button_texts(self):
        count = len(self.selected_paths)
        total_size = sum(
            f.size for files in self.all_results.values() for f in files if f.path in self.selected_paths
        )
        size_str = format_file_size(total_size)
        self.delete_btn.content = ft.Row([
            ft.Icon(ft.Icons.DELETE_SWEEP_ROUNDED, size=18, color="#FFFFFF"),
            ft.Text(f"{get_text('delete_selected', self.language)} ({count} • {size_str})", color="#FFFFFF", weight=ft.FontWeight.W_600)
        ], spacing=6, alignment=ft.MainAxisAlignment.CENTER)
        self.hardlink_btn.content = ft.Row([
            ft.Icon(ft.Icons.LINK_ROUNDED, size=18, color="#FFFFFF"),
            ft.Text(f"{get_text('hardlink_selected', self.language)} ({count})", color="#FFFFFF", weight=ft.FontWeight.W_600)
        ], spacing=6, alignment=ft.MainAxisAlignment.CENTER)
        if self.parent:
            try:
                self.delete_btn.update()
                self.hardlink_btn.update()
            except Exception:
                pass

    def _protect_originals(self):
        """C3: in a group where every file is selected the whole group would be
        deleted — keep the original (files[0], labelled 'Keep') in each such group."""
        for files in self.all_results.values():
            if len(files) > 1 and all(f.path in self.selected_paths for f in files):
                self.selected_paths.discard(files[0].path)

    def count_endangered_groups(self) -> int:
        """Groups currently selected for complete deletion (every file checked)."""
        return sum(
            1 for files in self.all_results.values()
            if len(files) > 1 and all(f.path in self.selected_paths for f in files)
        )

    def get_selected_entries(self) -> List[Tuple[str, int, float]]:
        """(path, size, mtime) snapshots for the selected files (C2)."""
        entries = []
        for path in self.selected_paths:
            info = self._path_to_info.get(path)
            if info is not None:
                entries.append((path, info.size, info.modified))
        return entries

    def apply_smart_selection(self, e):
        rule = self.smart_select_dropdown.value
        if rule == "invert":
            all_files_in_filtered = {f.path for files in self.filtered_results.values() for f in files}
            self.selected_paths = all_files_in_filtered - self.selected_paths
        else:
            self.selected_paths.clear()
            for files in self.filtered_results.values():
                if len(files) <= 1:
                    continue

                if rule == "first":
                    for f in files[1:]:
                        self.selected_paths.add(f.path)
                elif rule == "newest":
                    # The classic photo workflow: keep the newest copy.
                    keep = max(files, key=lambda x: x.modified)
                    for f in files:
                        if f.path != keep.path:
                            self.selected_paths.add(f.path)
                elif rule == "oldest":
                    keep = min(files, key=lambda x: x.modified)
                    for f in files:
                        if f.path != keep.path:
                            self.selected_paths.add(f.path)
                elif rule == "largest_res":
                    keep = self._pick_largest_resolution(files)
                    for f in files:
                        if f.path != keep.path:
                            self.selected_paths.add(f.path)
                elif rule == "priority":
                    # Keep copies that live in the user's priority folders,
                    # mark the rest. Groups with no priority member keep
                    # their first file (never the whole group).
                    priorities = load_priority_folders()
                    keep_paths = {f.path for f in files if is_priority_path(f.path, priorities)} or {files[0].path}
                    for f in files:
                        if f.path not in keep_paths:
                            self.selected_paths.add(f.path)
                elif rule == "last":
                    for f in files[:-1]:
                        self.selected_paths.add(f.path)
                elif rule == "shortest":
                    shortest = min(files, key=lambda x: len(x.path))
                    for f in files:
                        if f.path != shortest.path:
                            self.selected_paths.add(f.path)
                elif rule == "all":
                    for f in files:
                        self.selected_paths.add(f.path)
                elif rule == "none":
                    pass

        # C3: "all"/"invert" can leave whole groups selected — keep one copy.
        self._protect_originals()

        self.results_column.controls.clear()
        items = list(self.filtered_results.items())[:self.loaded_groups_count]
        for key, files in items:
            self.results_column.controls.append(self.build_group_card(key, files))

        self.update_action_button_texts()
        try:
            self.update()
        except Exception:
            pass

    def _page_or_none(self):
        """flet's Control.page raises RuntimeError (instead of returning None)
        for controls not added to a page — keeps headless use safe."""
        try:
            return self.page
        except RuntimeError:
            return None

    @staticmethod
    def _pick_largest_resolution(files: List[FileInfo]) -> FileInfo:
        """The image with the most pixels — the natural 'keep' candidate for
        photo series. PIL reads only the header for .size, so this stays fast
        even for large photos. Unreadable/non-image entries never raise."""
        best = files[0]
        best_pixels = -1
        for f in files:
            try:
                with Image.open(f.path) as img:
                    pixels = img.size[0] * img.size[1]
            except Exception:
                continue
            if pixels > best_pixels:
                best_pixels = pixels
                best = f
        return best

    def show_priority_folders_dialog(self, e):
        page = self._page_or_none()
        if page is None:
            return

        field = ft.TextField(
            value="\n".join(load_priority_folders()),
            label=get_text("folder_priorities", self.language),
            hint_text=get_text("folder_priorities_hint", self.language),
            multiline=True,
            min_lines=3,
            max_lines=6,
            border_color=BORDER_COLOR,
            bgcolor=SURFACE_HOVER,
            text_size=12
        )

        def save_and_close(_):
            folders = [line.strip() for line in (field.value or "").splitlines() if line.strip()]
            save_priority_folders(folders)
            page.pop_dialog()

        dlg = get_styled_dialog(
            title=get_text("folder_priorities", self.language),
            icon=ft.Icons.FOLDER_SPECIAL_ROUNDED,
            icon_color=PRIMARY_COLOR,
            content=ft.Column([field], tight=True, spacing=8, width=480),
            actions=[
                get_outlined_button(text=get_text("cancel", self.language), on_click=lambda _: page.pop_dialog()),
                get_primary_button(text=get_text("save", self.language), on_click=save_and_close, icon=ft.Icons.SAVE_OUTLINED)
            ]
        )
        page.show_dialog(dlg)

    def show_side_by_side_comparison(self, files: List[FileInfo]):
        page = self._page_or_none()
        if page is None or len(files) < 2:
            return

        file_a = files[0]
        file_b = files[1]

        def get_img_dims(path):
            try:
                with Image.open(path) as im:
                    return f"{im.width} × {im.height} px"
            except Exception:
                return get_text("unknown_size", self.language)

        dims_a = get_img_dims(file_a.path)
        dims_b = get_img_dims(file_b.path)

        def keep_a(e):
            self.selected_paths.discard(file_a.path)
            self.selected_paths.add(file_b.path)
            self.update_action_button_texts()
            page.pop_dialog()
            self.refresh_filtered_results()

        def keep_b(e):
            self.selected_paths.discard(file_b.path)
            self.selected_paths.add(file_a.path)
            self.update_action_button_texts()
            page.pop_dialog()
            self.refresh_filtered_results()

        card_a = ft.Container(
            content=ft.Column([
                ft.Text(get_text("photo_left_title", self.language), weight=ft.FontWeight.BOLD, color=SUCCESS_COLOR),
                ft.Image(src=file_a.path, width=320, height=240, fit=ft.BoxFit.CONTAIN, border_radius=6),
                ft.Text(os.path.basename(file_a.path), size=12, weight=ft.FontWeight.BOLD),
                ft.Text(f"{get_text('dimensions', self.language)}: {dims_a}", size=11, color=TEXT_MUTED),
                ft.Text(f"{get_text('size', self.language)}: {format_file_size(file_a.size)}", size=11, color=TEXT_MUTED),
                get_primary_button(text=get_text("keep_left_btn", self.language), on_click=keep_a, bgcolor=SUCCESS_COLOR, height=36)
            ], spacing=6),
            padding=10,
            bgcolor=SURFACE_HOVER,
            border_radius=8,
            expand=True
        )

        card_b = ft.Container(
            content=ft.Column([
                ft.Text(get_text("photo_right_title", self.language), weight=ft.FontWeight.BOLD, color=PRIMARY_COLOR),
                ft.Image(src=file_b.path, width=320, height=240, fit=ft.BoxFit.CONTAIN, border_radius=6),
                ft.Text(os.path.basename(file_b.path), size=12, weight=ft.FontWeight.BOLD),
                ft.Text(f"{get_text('dimensions', self.language)}: {dims_b}", size=11, color=TEXT_MUTED),
                ft.Text(f"{get_text('size', self.language)}: {format_file_size(file_b.size)}", size=11, color=TEXT_MUTED),
                get_primary_button(text=get_text("keep_right_btn", self.language), on_click=keep_b, bgcolor=PRIMARY_COLOR, height=36)
            ], spacing=6),
            padding=10,
            bgcolor=SURFACE_HOVER,
            border_radius=8,
            expand=True
        )

        dlg = get_styled_dialog(
            title=get_text("compare_modal_title", self.language),
            icon=ft.Icons.COMPARE_ROUNDED,
            icon_color=PRIMARY_COLOR,
            content=ft.Container(
                content=ft.Row([card_a, card_b], spacing=12),
                width=720,
                height=420
            ),
            actions=[
                get_outlined_button(text=get_text("close", self.language), on_click=lambda _: page.pop_dialog())
            ]
        )
        page.show_dialog(dlg)

    def show_image_lightbox(self, image_path: str):
        page = self._page_or_none()
        if page is None:
            return

        def close_modal(e):
            page.pop_dialog()

        # 2.1d progressive open: the cached thumbnail is a small JPEG that is
        # usually already generated for the result rows, so the dialog paints
        # instantly; the full-size file is swapped in right after.
        # get_cached_thumbnail itself falls back to the original path when no
        # preview can be made (corrupt image, read-only cache dir) — the old
        # behaviour is preserved.
        image = ft.Image(src=get_cached_thumbnail(image_path), fit=ft.BoxFit.CONTAIN)

        dlg = get_styled_dialog(
            title=os.path.basename(image_path),
            icon=ft.Icons.IMAGE_ROUNDED,
            icon_color=PRIMARY_COLOR,
            content=ft.Container(
                content=image,
                width=600,
                height=450,
            ),
            actions=[
                get_outlined_button(text=get_text("close", self.language), on_click=close_modal),
                get_primary_button(text=get_text("open_file", self.language), on_click=lambda _: self.open_file_natively(image_path), icon=ft.Icons.OPEN_IN_NEW_ROUNDED)
            ]
        )
        page.show_dialog(dlg)

        def upgrade_to_full_size():
            try:
                image.src = image_path
                image.update()
            except Exception as ex:
                # The dialog keeps showing the thumbnail it opened with.
                logger.debug("lightbox full-size swap failed for %s: %s", image_path, ex)

        timer = threading.Timer(self.LIGHTBOX_FULL_IMAGE_DELAY, upgrade_to_full_size)
        timer.daemon = True
        timer.start()

    def open_file_natively(self, path: str):
        try:
            os.startfile(path)
        except Exception as ex:
            logger.warning("Error opening file '%s': %s", path, ex)

    def open_in_explorer(self, path: str):
        try:
            os.startfile(os.path.dirname(path))
        except Exception as ex:
            logger.warning("Error opening directory '%s': %s", path, ex)

    def on_delete_clicked(self, e):
        if not self.selected_paths:
            return
        if self._operation_busy:
            return

        selected_list = list(self.selected_paths)
        system_files = [p for p in selected_list if is_system_path(p)]
        total_size = sum(
            f.size for files in self.all_results.values() for f in files if f.path in self.selected_paths
        )
        endangered = self.count_endangered_groups()
        page = self._page_or_none()
        if page is None:
            return

        def confirm_delete(use_trash: bool):
            page.pop_dialog()
            # C3: keep at least one copy in every fully-selected group.
            self._protect_originals()
            selected_entries = self.get_selected_entries()
            if not selected_entries:
                return
            # Show progress bar
            self.operation_progress.visible = True
            self.operation_progress.value = 0
            self.operation_status.visible = True
            self.operation_status.value = get_text("deleting", self.language).format(0, len(selected_entries))
            try:
                self.update()
            except Exception:
                pass
            self._operation_busy = True
            self.on_delete(selected_entries, use_trash=use_trash)

        def cancel_dialog(e):
            page.pop_dialog()

        # Without send2trash the "to Recycle Bin" option is a lie — disable it
        # so the confirm dialog explicitly means permanent deletion.
        trash_checkbox = ft.Checkbox(
            label=get_text("send_to_trash_label", self.language),
            value=self.trash_default and self.trash_available,
            disabled=not self.trash_available
        )

        content_controls = [
            ft.Text(get_text("delete_summary_msg", self.language).format(len(selected_list), format_file_size(total_size)), size=14),
            trash_checkbox
        ]

        if endangered:
            content_controls.insert(0,
                ft.Container(
                    content=ft.Text(get_text("delete_all_selected_warning", self.language).format(endangered), color=WARNING_COLOR, size=12),
                    bgcolor=f"{WARNING_COLOR}22",
                    padding=10,
                    border_radius=8
                )
            )

        if system_files:
            sys_summary = "\n".join([f"• {os.path.basename(f)}" for f in system_files[:4]])
            if len(system_files) > 4:
                sys_summary += "\n" + get_text("more_items_suffix", self.language).format(len(system_files) - 4)
            content_controls.insert(0,
                ft.Container(
                    content=ft.Text(get_text("system_delete_warning", self.language).format(sys_summary), color=DANGER_COLOR, size=12),
                    bgcolor=f"{DANGER_COLOR}22",
                    padding=10,
                    border_radius=8
                )
            )

        dlg = get_styled_dialog(
            title=get_text("confirm_deletion_title", self.language),
            icon=ft.Icons.DELETE_FOREVER_ROUNDED,
            icon_color=DANGER_COLOR,
            content=ft.Column(content_controls, tight=True, spacing=12),
            actions=[
                get_outlined_button(text=get_text("cancel", self.language), on_click=cancel_dialog),
                get_primary_button(
                    text=get_text("delete", self.language),
                    on_click=lambda _: confirm_delete(use_trash=trash_checkbox.value),
                    bgcolor=DANGER_COLOR,
                    icon=ft.Icons.DELETE_ROUNDED
                )
            ]
        )
        page.show_dialog(dlg)

    def on_hardlink_clicked(self, e):
        if not self.selected_paths or not self.on_hardlink:
            return
        if self._operation_busy:
            return

        selected_list = list(self.selected_paths)
        total_size = sum(
            f.size for files in self.all_results.values() for f in files if f.path in self.selected_paths
        )
        page = self._page_or_none()
        if page is None:
            return

        def confirm_hardlink(_):
            page.pop_dialog()
            # Show progress bar
            self.operation_progress.visible = True
            self.operation_progress.value = 0
            self.operation_status.visible = True
            self.operation_status.value = get_text("hardlinking", self.language).format(0, len(selected_list))
            try:
                self.update()
            except Exception:
                pass
            self._operation_busy = True
            groups_map = {}
            for files in self.all_results.values():
                if not files:
                    continue
                original = files[0].path
                # (path, size, mtime) snapshots — the worker re-verifies (C2).
                # 0-byte files are skipped: hardlinking them frees exactly
                # nothing and clutters the success stats.
                dupes = [
                    (f.path, f.size, f.modified)
                    for f in files[1:]
                    if f.path in self.selected_paths and f.size > 0
                ]
                if dupes:
                    groups_map[original] = dupes

            self.on_hardlink(groups_map)

        dlg = get_styled_dialog(
            title=get_text("hardlink_confirm_title", self.language),
            icon=ft.Icons.LINK_ROUNDED,
            icon_color=PRIMARY_COLOR,
            content=ft.Text(get_text("hardlink_confirm_msg", self.language).format(len(selected_list), format_file_size(total_size)), size=14),
            actions=[
                get_outlined_button(text=get_text("cancel", self.language), on_click=lambda _: page.pop_dialog()),
                get_primary_button(
                    text=get_text("hardlink_selected", self.language),
                    on_click=confirm_hardlink,
                    bgcolor=PRIMARY_COLOR,
                    icon=ft.Icons.LINK_ROUNDED
                )
            ]
        )
        page.show_dialog(dlg)

    async def on_move_clicked(self, e):
        """Pick a destination, confirm, then hand (path, size, mtime)
        snapshots to the move worker — the reversible alternative to
        deletion."""
        if not self.selected_paths or not self.on_move:
            return
        if self._operation_busy:
            return
        page = self._page_or_none()
        if page is None:
            return

        destination = await self.move_picker.get_directory_path(
            dialog_title=get_text("choose_destination", self.language)
        )
        if not destination:
            return

        selected_list = list(self.selected_paths)
        total_size = sum(
            f.size for files in self.all_results.values() for f in files if f.path in self.selected_paths
        )

        def confirm_move(_):
            page.pop_dialog()
            self._do_move(destination)

        dlg = get_styled_dialog(
            title=get_text("move_confirm_title", self.language),
            icon=ft.Icons.DRIVE_FILE_MOVE_ROUNDED,
            icon_color=PRIMARY_COLOR,
            content=ft.Text(
                get_text("move_confirm_msg", self.language).format(len(selected_list), format_file_size(total_size), destination),
                size=13, color=TEXT_SECONDARY
            ),
            actions=[
                get_outlined_button(text=get_text("cancel", self.language), on_click=lambda _: page.pop_dialog()),
                get_primary_button(
                    text=get_text("move_selected", self.language),
                    on_click=confirm_move,
                    icon=ft.Icons.DRIVE_FILE_MOVE_ROUNDED
                )
            ]
        )
        page.show_dialog(dlg)

    def _do_move(self, destination: str):
        """Confirm-handler core for "Move to folder" — extracted so the C3
        protection is testable headless. Never moves a group out entirely:
        at least one copy stays in its original location."""
        # C3 for moves too: a fully-selected group keeps its original.
        self._protect_originals()
        selected_entries = self.get_selected_entries()
        if not selected_entries:
            return
        self.operation_progress.visible = True
        self.operation_progress.value = 0
        self.operation_status.visible = True
        self.operation_status.value = get_text("move_success_msg", self.language).format(0)
        try:
            self.update()
        except Exception:
            pass
        self._operation_busy = True
        self.on_move(selected_entries, destination)

    def remove_files(self, removed_paths: List[str]):
        removed_set = set(removed_paths)
        self.selected_paths -= removed_set

        new_all_results = {}
        for key, files in self.all_results.items():
            remaining = [f for f in files if f.path not in removed_set]
            if len(remaining) > 1:
                new_all_results[key] = remaining

        self.all_results = new_all_results
        self.total_groups = len(self.all_results)
        self.total_dupe_files = sum(max(0, len(files) - 1) for files in self.all_results.values())
        self.total_wasted_bytes = compute_wasted_bytes(self.all_results)
        self._path_to_info = {f.path: f for files in self.all_results.values() for f in files}

        self.category_counts = {"all": self.total_groups}
        self.category_wasted = {}
        for files in self.all_results.values():
            if files:
                cat = files[0].category
                self.category_counts[cat] = self.category_counts.get(cat, 0) + 1
                self.category_wasted[cat] = self.category_wasted.get(cat, 0) + sum(f.size for f in files[1:])

        self.build_ui()
        self.refresh_filtered_results()

    async def trigger_export(self, export_type: str):
        self.pending_export_type = export_type
        save_path = await self.export_picker.save_file(
            dialog_title=get_text(f"export_{export_type}", self.language),
            file_name=f"duplicates_report.{export_type}",
            file_type=ft.FilePickerFileType.CUSTOM,
            allowed_extensions=[export_type]
        )
        if save_path:
            self.write_export_file(save_path, export_type)

    def write_export_file(self, path: str, export_type: str):
        try:
            if export_type == "csv":
                # BOM: Excel (the default CSV viewer for this audience) opens
                # plain utf-8 as ANSI and mangles Cyrillic paths.
                with open(path, 'w', newline='', encoding='utf-8-sig') as f:
                    writer = csv.writer(f)
                    writer.writerow(["Group", "File Name", "Path", "Size (bytes)", "Size (formatted)", "Modified", "Category"])
                    for key, files in self.all_results.items():
                        for file in files:
                            writer.writerow([
                                key,
                                file.name,
                                file.path,
                                file.size,
                                format_file_size(file.size),
                                datetime.datetime.fromtimestamp(file.modified).strftime("%Y-%m-%d %H:%M:%S"),
                                file.category
                            ])
            elif export_type == "json":
                data = {
                    "total_groups": self.total_groups,
                    "wasted_space": self.total_wasted_bytes,
                    "groups": {
                        k: [{"name": f.name, "path": f.path, "size": f.size, "modified": f.modified, "category": f.category} for f in files]
                        for k, files in self.all_results.items()
                    }
                }
                with open(path, 'w', encoding='utf-8') as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
            elif export_type == "txt":
                with open(path, 'w', encoding='utf-8') as f:
                    f.write(f"DUPLICATER REPORT - {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}\n")
                    f.write(f"Total Duplicate Groups: {self.total_groups}\n")
                    f.write(f"Wasted Space: {format_file_size(self.total_wasted_bytes)}\n\n")
                    for key, files in self.all_results.items():
                        f.write(f"=== Group: {key} ({len(files)} files, {format_file_size(files[0].size)} each) ===\n")
                        for file in files:
                            f.write(f"  - {file.path}\n")
                        f.write("\n")
            elif export_type == "html":
                # Standalone self-contained report: opens in any browser,
                # printable — handy to review the plan before cleaning.
                group_rows = []
                for key, files in self.all_results.items():
                    file_rows = "".join(
                        "<tr>"
                        f"<td>{html.escape(file.name)}</td>"
                        f"<td>{html.escape(file.path)}</td>"
                        f"<td class='num'>{file.size}</td>"
                        f"<td class='num'>{html.escape(format_file_size(file.size))}</td>"
                        "</tr>"
                        for file in files
                    )
                    reclaimable = format_file_size(sum(x.size for x in files[1:]))
                    group_rows.append(
                        f"<tr class='group'><td colspan='4'>{html.escape(key)} — {len(files)} files, {html.escape(reclaimable)}</td></tr>"
                        + file_rows
                    )
                document = (
                    "<!DOCTYPE html><html><head><meta charset='utf-8'>"
                    "<title>Duplicater Report</title><style>"
                    "body{font-family:'Segoe UI',Arial,sans-serif;margin:24px;background:#0f1218;color:#e6e9ef}"
                    "h1{font-size:20px;margin-bottom:4px}p{color:#8b93a7;font-size:12px}"
                    "table{border-collapse:collapse;width:100%;font-size:12px}"
                    "th,td{border:1px solid #2a324b;padding:5px 9px;text-align:left;vertical-align:top}"
                    "th{background:#1a2030}.num{text-align:right;white-space:nowrap}"
                    ".group td{background:#1a2030;font-weight:600}"
                    "</style></head><body>"
                    "<h1>Duplicater Report</h1>"
                    f"<p>{self.total_groups} groups · {html.escape(format_file_size(self.total_wasted_bytes))} reclaimable · v{APP_VERSION}</p>"
                    "<table><tr><th>File</th><th>Path</th><th>Size (bytes)</th><th>Size</th></tr>"
                    + "".join(group_rows) +
                    "</table></body></html>"
                )
                with open(path, 'w', encoding='utf-8') as f:
                    f.write(document)
        except Exception as ex:
            logger.warning("Export error for '%s': %s", path, ex)
