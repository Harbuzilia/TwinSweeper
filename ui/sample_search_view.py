import flet as ft
import os
import datetime
from typing import List, Optional

from scanner import format_file_size, get_file_category
from ui.components import (
    get_styled_card, get_primary_button, get_outlined_button, get_header_row, get_badge,
    get_drive_chip, get_folder_list_item, get_progress_card, format_path_short, get_detected_drives,
    PRIMARY_COLOR, ACCENT_COLOR, DANGER_COLOR,
    TEXT_PRIMARY, TEXT_SECONDARY, TEXT_MUTED, SURFACE_HOVER, BORDER_COLOR,
    CATEGORY_ICONS
)
from locales import get_text

class SampleSearchView(ft.Column):
    def __init__(self, on_scan_start, language="ru"):
        super().__init__()
        self.on_scan_start = on_scan_start
        self.language = language

        self.sample_path: Optional[str] = None
        self.search_directories: List[str] = []
        self.cancel_callback = None

        self.scroll = ft.ScrollMode.AUTO
        self.expand = True
        self.spacing = 14

        # File Pickers
        self.pick_sample_dialog = ft.FilePicker()
        self.pick_folders_dialog = ft.FilePicker()

        # UI Components
        self.sample_info_container = ft.Container(
            content=ft.Row([
                ft.Icon(ft.Icons.ATTACH_FILE_ROUNDED, color=TEXT_MUTED, size=22),
                ft.Text(get_text("no_sample_selected", self.language), color=TEXT_MUTED, size=13, weight=ft.FontWeight.W_500),
            ], alignment=ft.MainAxisAlignment.CENTER, spacing=10),
            padding=ft.Padding.symmetric(vertical=20, horizontal=16),
            bgcolor=SURFACE_HOVER,
            border=ft.Border.all(1, BORDER_COLOR),
            border_radius=10
        )

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
        # Matching Criteria
        self.check_size = ft.Checkbox(label=get_text("match_size", self.language), value=True)
        self.check_hash = ft.Checkbox(label=get_text("match_hash", self.language), value=True)
        self.check_byte = ft.Checkbox(label=get_text("match_byte", self.language), value=False)
        self.check_name = ft.Checkbox(label=get_text("match_name", self.language), value=False)

        # Progress UI
        self.progress_bar = ft.ProgressBar(value=0, color=PRIMARY_COLOR, bgcolor=SURFACE_HOVER, visible=False)
        self.status_text = ft.Text("", size=13, color=TEXT_SECONDARY)
        self.status_icon = ft.Icon(ft.Icons.HOURGLASS_TOP_ROUNDED, color=PRIMARY_COLOR, size=18, visible=False)

        self.start_button = get_primary_button(
            text=get_text("start_scan", self.language),
            on_click=self.start_scan,
            icon=ft.Icons.SEARCH_ROUNDED,
            height=46
        )

        self.cancel_button = get_outlined_button(
            text=get_text("cancel_scan", self.language),
            on_click=self.on_cancel_click,
            icon=ft.Icons.CLOSE_ROUNDED,
            color=DANGER_COLOR,
            border_color=DANGER_COLOR,
            height=46
        )
        self.cancel_button.visible = False

        self.build_ui()

    def get_detected_drives(self) -> list[str]:
        return get_detected_drives()

    def add_drive(self, drive: str):
        if drive not in self.search_directories:
            self.search_directories.append(drive)
            self.update_folders_list()

    def build_ui(self):
        drive_chips = [
            get_drive_chip(
                drive_label=d,
                on_click=lambda _, drv=d: self.add_drive(drv),
                is_selected=(d in self.search_directories)
            )
            for d in self.get_detected_drives()
        ]

        self.controls = [
            # Header
            get_header_row(
                title=get_text("search_by_sample", self.language),
                subtitle=get_text("search_by_sample_desc", self.language)
            ),

            # Step 1: Sample File Card
            get_styled_card(
                ft.Column([
                    ft.Row([
                        ft.Icon(ft.Icons.FINGERPRINT_ROUNDED, color=PRIMARY_COLOR, size=20),
                        ft.Text(get_text("step_1_sample", self.language), size=15, weight=ft.FontWeight.BOLD, color=TEXT_PRIMARY),
                    ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
                    ft.Row([
                        get_primary_button(
                            text=get_text("choose_file", self.language),
                            on_click=self.pick_sample,
                            icon=ft.Icons.UPLOAD_FILE_ROUNDED,
                            bgcolor=PRIMARY_COLOR
                        )
                    ]),
                    self.sample_info_container
                ], spacing=10)
            ),

            # Step 2: Target Folders Card
            get_styled_card(
                ft.Column([
                    ft.Row([
                        ft.Icon(ft.Icons.FOLDER_SPECIAL_ROUNDED, color=ACCENT_COLOR, size=20),
                        ft.Text(get_text("step_2_scope", self.language), size=15, weight=ft.FontWeight.BOLD, color=TEXT_PRIMARY),
                        get_badge(f"{len(self.search_directories)}", color=ACCENT_COLOR),
                        ft.Container(expand=True),
                        ft.Row([ft.Text(get_text("quick_drive_add", self.language), size=12, color=TEXT_MUTED, weight=ft.FontWeight.W_500)] + drive_chips, spacing=6, vertical_alignment=ft.CrossAxisAlignment.CENTER)
                    ]),
                    ft.Row([
                        get_primary_button(
                            text=get_text("choose_folder", self.language),
                            on_click=self.pick_folder,
                            icon=ft.Icons.CREATE_NEW_FOLDER_OUTLINED,
                            bgcolor=ACCENT_COLOR
                        )
                    ]),
                    self.folders_container if self.search_directories else self.folders_empty_hint
                ], spacing=10)
            ),

            # Criteria Card
            get_styled_card(
                ft.Column([
                    ft.Text(get_text("match_criteria", self.language), size=14, weight=ft.FontWeight.W_600, color=TEXT_PRIMARY),
                    ft.Row([self.check_size, self.check_hash, self.check_name, self.check_byte], wrap=True)
                ], spacing=8)
            ),

            # Action & Progress Card
            get_progress_card(
                status_icon=self.status_icon,
                status_text=self.status_text,
                progress_bar=self.progress_bar,
                primary_action_btn=self.start_button,
                secondary_action_btn=self.cancel_button
            )
        ]

    async def pick_sample(self, e):
        files = await self.pick_sample_dialog.pick_files(allow_multiple=False)
        if files and files[0].path:
            self.sample_path = files[0].path
            self.update_sample_card()

    def update_sample_card(self):
        if not self.sample_path or not os.path.exists(self.sample_path):
            return

        stat = os.stat(self.sample_path)
        cat = get_file_category(os.path.basename(self.sample_path))
        cat_icon, cat_color = CATEGORY_ICONS.get(cat, CATEGORY_ICONS["other"])
        mod_date = datetime.datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M")

        self.sample_info_container = ft.Container(
            content=ft.Row([
                ft.Icon(cat_icon, color=cat_color, size=32),
                ft.Column([
                    ft.Text(os.path.basename(self.sample_path), weight=ft.FontWeight.BOLD, size=14, color=TEXT_PRIMARY),
                    ft.Text(format_path_short(self.sample_path), size=12, color=TEXT_MUTED, tooltip=self.sample_path),
                    ft.Row([
                        get_badge(format_file_size(stat.st_size), color=PRIMARY_COLOR),
                        get_badge(mod_date, color=TEXT_SECONDARY),
                    ], spacing=6)
                ], spacing=3, expand=True)
            ], vertical_alignment=ft.CrossAxisAlignment.CENTER),
            bgcolor=SURFACE_HOVER,
            border=ft.Border.all(1, BORDER_COLOR),
            border_radius=10,
            padding=14
        )
        self.build_ui()
        self.update()

    async def pick_folder(self, e):
        path = await self.pick_folders_dialog.get_directory_path()
        if path and path not in self.search_directories:
            self.search_directories.append(path)
            self.update_folders_list()

    def remove_folder(self, folder):
        if folder in self.search_directories:
            self.search_directories.remove(folder)
            self.update_folders_list()

    def update_folders_list(self):
        self.folders_container.controls.clear()
        for folder in self.search_directories:
            self.folders_container.controls.append(
                get_folder_list_item(
                    path=folder,
                    on_remove=lambda _, f=folder: self.remove_folder(f),
                    badge_color=ACCENT_COLOR,
                    icon_color=ACCENT_COLOR,
                    remove_tooltip=get_text("remove", self.language)
                )
            )
        self.build_ui()
        self.update()

    def start_scan(self, e):
        if not self.sample_path:
            self.status_text.value = get_text("error_no_sample", self.language)
            self.status_text.color = DANGER_COLOR
            self.status_icon.visible = True
            self.status_icon.name = ft.Icons.ERROR_OUTLINE_ROUNDED
            self.status_icon.color = DANGER_COLOR
            self.update()
            return

        if not self.search_directories:
            self.status_text.value = get_text("error_no_scope", self.language)
            self.status_text.color = DANGER_COLOR
            self.status_icon.visible = True
            self.status_icon.name = ft.Icons.ERROR_OUTLINE_ROUNDED
            self.status_icon.color = DANGER_COLOR
            self.update()
            return

        self.start_button.visible = False
        self.cancel_button.visible = True
        self.progress_bar.visible = True
        self.progress_bar.value = None
        self.status_text.value = get_text("scanning", self.language)
        self.status_text.color = TEXT_SECONDARY
        self.status_icon.visible = True
        self.status_icon.name = ft.Icons.HOURGLASS_TOP_ROUNDED
        self.status_icon.color = PRIMARY_COLOR
        self.update()

        def setup_cancel(cancel_fn):
            self.cancel_callback = cancel_fn

        self.on_scan_start(
            sample_path=self.sample_path,
            directories=self.search_directories,
            by_name=self.check_name.value,
            by_size=self.check_size.value,
            by_hash=self.check_hash.value,
            by_byte=self.check_byte.value,
            progress_callback=self.update_status,
            on_cancel_setup=setup_cancel,
            on_scan_finished=self.on_scan_finished
        )

    def on_scan_finished(self):
        self.start_button.visible = True
        self.cancel_button.visible = False
        self.progress_bar.visible = False
        self.status_icon.visible = False
        if self.parent:
            try:
                self.update()
            except Exception:
                pass

    def update_status(self, message: str, percent: float = None):
        self.status_text.value = message
        if percent is not None:
            self.progress_bar.value = max(0.0, min(1.0, percent))
        else:
            self.progress_bar.value = None
        if self.parent:
            try:
                self.update()
            except Exception:
                pass

    def on_cancel_click(self, e):
        if self.cancel_callback:
            self.cancel_callback()
        self.status_text.value = get_text("scan_cancelled", self.language)
        self.progress_bar.visible = False
        self.start_button.visible = True
        self.cancel_button.visible = False
        self.status_icon.visible = False
        self.update()
