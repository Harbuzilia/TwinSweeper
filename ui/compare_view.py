import flet as ft
import os
from typing import Optional

from scanner import format_file_size
from ui.components import (
    SIMILARITY_COLOR,
    get_styled_card, get_stat_card, get_primary_button, get_header_row, get_badge,
    get_segmented_control, get_drive_chip, get_progress_card, format_path_short, get_detected_drives,
    PRIMARY_COLOR, ACCENT_COLOR, SUCCESS_COLOR, WARNING_COLOR, DANGER_COLOR, INFO_COLOR,
    TEXT_PRIMARY, TEXT_SECONDARY, TEXT_MUTED, BORDER_COLOR, SURFACE_HOVER
)
from locales import get_text

class CompareView(ft.Column):
    def __init__(self, on_compare_start, language="ru"):
        super().__init__()
        self.on_compare_start = on_compare_start
        self.language = language

        self.folder_a: Optional[str] = None
        self.folder_b: Optional[str] = None
        self.comparison_data = None
        self.active_tab = "common"

        self.scroll = ft.ScrollMode.AUTO
        self.expand = True
        self.spacing = 14

        # File Pickers
        self.pick_a_dialog = ft.FilePicker()
        self.pick_b_dialog = ft.FilePicker()

        # UI State
        self.folder_a_display = ft.Text(get_text("no_folder_a", language), size=13, color=TEXT_MUTED)
        self.folder_b_display = ft.Text(get_text("no_folder_b", language), size=13, color=TEXT_MUTED)

        self.progress_bar = ft.ProgressBar(value=None, color=PRIMARY_COLOR, bgcolor=SURFACE_HOVER, visible=False)
        self.status_text = ft.Text("", size=13, color=TEXT_SECONDARY)

        self.results_container = ft.Column(spacing=12)

        self.build_ui()

    def get_detected_drives(self) -> list[str]:
        return get_detected_drives()

    def build_ui(self):
        drives = self.get_detected_drives()
        chips_a = [
            get_drive_chip(
                drive_label=d,
                on_click=lambda _, drv=d: self.set_folder_a(drv),
                is_selected=(self.folder_a == d),
                accent_color=PRIMARY_COLOR
            )
            for d in drives
        ]
        chips_b = [
            get_drive_chip(
                drive_label=d,
                on_click=lambda _, drv=d: self.set_folder_b(drv),
                is_selected=(self.folder_b == d),
                accent_color=ACCENT_COLOR
            )
            for d in drives
        ]

        self.controls = [
            # Header
            get_header_row(
                title=get_text("compare_folders", self.language),
                subtitle=get_text("compare_desc", self.language)
            ),

            # Folder A and B Selection Cards (Side-by-side)
            ft.Row([
                ft.Container(
                    content=get_styled_card(
                        ft.Column([
                            ft.Row([
                                ft.Icon(ft.Icons.FOLDER_SPECIAL_ROUNDED, color=PRIMARY_COLOR, size=20),
                                ft.Text(get_text("folder_a", self.language), size=14, weight=ft.FontWeight.BOLD, color=TEXT_PRIMARY),
                                ft.Container(expand=True),
                                ft.Row([ft.Text(get_text("quick_drive_add", self.language), size=11, color=TEXT_MUTED, weight=ft.FontWeight.W_500)] + chips_a, spacing=4, vertical_alignment=ft.CrossAxisAlignment.CENTER)
                            ]),
                            get_primary_button(
                                text=get_text("choose_folder", self.language),
                                on_click=self.pick_folder_a,
                                icon=ft.Icons.FOLDER_OPEN_ROUNDED,
                                bgcolor=PRIMARY_COLOR
                            ),
                            self.folder_a_display
                        ], spacing=10)
                    ),
                    expand=True
                ),
                ft.Container(
                    content=get_styled_card(
                        ft.Column([
                            ft.Row([
                                ft.Icon(ft.Icons.FOLDER_SPECIAL_ROUNDED, color=ACCENT_COLOR, size=20),
                                ft.Text(get_text("folder_b", self.language), size=14, weight=ft.FontWeight.BOLD, color=TEXT_PRIMARY),
                                ft.Container(expand=True),
                                ft.Row([ft.Text(get_text("quick_drive_add", self.language), size=11, color=TEXT_MUTED, weight=ft.FontWeight.W_500)] + chips_b, spacing=4, vertical_alignment=ft.CrossAxisAlignment.CENTER)
                            ]),
                            get_primary_button(
                                text=get_text("choose_folder", self.language),
                                on_click=self.pick_folder_b,
                                icon=ft.Icons.FOLDER_OPEN_ROUNDED,
                                bgcolor=ACCENT_COLOR
                            ),
                            self.folder_b_display
                        ], spacing=10)
                    ),
                    expand=True
                ),
            ], spacing=12),

            # Compare Button & Progress
            get_progress_card(
                status_icon=ft.Icon(ft.Icons.COMPARE_ARROWS_ROUNDED, size=18, color=PRIMARY_COLOR),
                status_text=self.status_text,
                progress_bar=self.progress_bar,
                primary_action_btn=get_primary_button(
                    text=get_text("start_compare", self.language),
                    on_click=self.start_compare,
                    icon=ft.Icons.COMPARE_ARROWS_ROUNDED,
                    height=46
                )
            ),

            # Results Section
            self.results_container
        ]

    def set_folder_a(self, path: str):
        self.folder_a = path
        self.folder_a_display.value = path
        self.folder_a_display.color = TEXT_PRIMARY
        self.folder_a_display.weight = ft.FontWeight.W_500
        self.update()

    def set_folder_b(self, path: str):
        self.folder_b = path
        self.folder_b_display.value = path
        self.folder_b_display.color = TEXT_PRIMARY
        self.folder_b_display.weight = ft.FontWeight.W_500
        self.update()

    async def pick_folder_a(self, e):
        path = await self.pick_a_dialog.get_directory_path()
        if path:
            self.set_folder_a(path)

    async def pick_folder_b(self, e):
        path = await self.pick_b_dialog.get_directory_path()
        if path:
            self.set_folder_b(path)

    def start_compare(self, e):
        if not self.folder_a or not self.folder_b:
            self.status_text.value = get_text("error_select_both", self.language)
            self.status_text.color = DANGER_COLOR
            self._safe_update()
            return

        if not os.path.isdir(self.folder_a) or not os.path.isdir(self.folder_b):
            self.status_text.value = get_text("error_folder_not_found", self.language)
            self.status_text.color = DANGER_COLOR
            self._safe_update()
            return

        self.progress_bar.visible = True
        self.status_text.value = get_text("comparing", self.language)
        self.status_text.color = TEXT_SECONDARY
        self.results_container.controls.clear()
        self._safe_update()

        self.on_compare_start(
            self.folder_a,
            self.folder_b,
            self.show_results,
            self.update_status,
            self.show_error
        )

    def _safe_update(self):
        """Headless/unmounted-safe update — flet's Control.page raises
        RuntimeError (not None) for controls not yet added to a page."""
        if self.parent is not None:
            try:
                self.update()
            except RuntimeError:
                pass

    def update_status(self, message: str):
        self.status_text.value = message
        self._safe_update()

    def show_error(self, message: str):
        """A failed comparison is an error state, not an empty result —
        hiding the progress bar and coloring the status red tells the user
        something actually went wrong."""
        self.progress_bar.visible = False
        self.status_text.value = message
        self.status_text.color = DANGER_COLOR
        self._safe_update()

    def show_results(self, results: dict):
        if self.parent is None:
            return
        self.progress_bar.visible = False
        self.status_text.value = ""
        self.comparison_data = results

        unique_a = results["unique_a"]
        unique_b = results["unique_b"]
        common = results["common"]
        total = results["total_files"]

        overlap_pct = (len(common) / total * 100) if total > 0 else 0
        identical_count = sum(1 for c in common if c["similarity"] == 1.0)
        modified_count = len(common) - identical_count

        tab_options = [
            ("common", f"{get_text('common_files', self.language)} ({len(common)})", ft.Icons.COMPARE_ROUNDED),
            ("unique_a", f"{get_text('unique_in_a', self.language)} ({len(unique_a)})", ft.Icons.FOLDER_OUTLINED),
            ("unique_b", f"{get_text('unique_in_b', self.language)} ({len(unique_b)})", ft.Icons.FOLDER_OUTLINED),
        ]

        self.results_container.controls = [
            # Stats Cards
            ft.Row([
                get_stat_card(
                    title=get_text("overlap", self.language),
                    value=f"{overlap_pct:.1f}%",
                    subtitle=get_text("compare_files_count", self.language).format(len(common), total),
                    icon=ft.Icons.PIE_CHART_OUTLINE_ROUNDED,
                    icon_color=PRIMARY_COLOR
                ),
                get_stat_card(
                    title=get_text("unique_in_a", self.language),
                    value=str(len(unique_a)),
                    subtitle=os.path.basename(self.folder_a),
                    icon=ft.Icons.FOLDER_OUTLINED,
                    icon_color=INFO_COLOR
                ),
                get_stat_card(
                    title=get_text("unique_in_b", self.language),
                    value=str(len(unique_b)),
                    subtitle=os.path.basename(self.folder_b),
                    icon=ft.Icons.FOLDER_OUTLINED,
                    icon_color=ACCENT_COLOR
                ),
            ], spacing=12),

            # Progress Bar for Overlap
            get_styled_card(
                ft.Column([
                    ft.Row([
                        ft.Text(f"{get_text('overlap', self.language)}: {overlap_pct:.1f}%", weight=ft.FontWeight.BOLD, size=14, color=TEXT_PRIMARY),
                        get_badge(f"{identical_count} {get_text('identical', self.language)}", color=SUCCESS_COLOR),
                        get_badge(f"{modified_count} {get_text('different_content', self.language)}", color=WARNING_COLOR) if modified_count > 0 else ft.Container()
                    ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
                    ft.ProgressBar(value=overlap_pct / 100.0, color=SUCCESS_COLOR, bgcolor=BORDER_COLOR)
                ], spacing=8)
            ),

            # Segmented Pill Tabs for Details
            ft.Row([
                get_segmented_control(
                    options=tab_options,
                    selected_key=self.active_tab,
                    on_select=self.set_active_tab
                )
            ]),

            # Dynamic Tab Content
            self.build_active_tab_content()
        ]
        if self.parent is not None:
            try:
                self.update()
            except RuntimeError:
                pass

    def set_active_tab(self, tab: str):
        self.active_tab = tab
        if self.comparison_data:
            self.results_container.controls[-1] = self.build_active_tab_content()
            self.update()

    def build_active_tab_content(self) -> ft.Container:
        if not self.comparison_data:
            return ft.Container()

        rows = []
        if self.active_tab == "common":
            items = self.comparison_data["common"]
            if not items:
                rows.append(ft.Text(get_text("compare_no_common", self.language), color=TEXT_MUTED))
            for item in items:
                # Real compare_folders schema (H1): every common entry carries
                # two FileInfo objects (file_a/file_b) plus similarity/newer/larger.
                fa = item["file_a"]
                fb = item["file_b"]
                sim = item["similarity"]
                badge_color = SUCCESS_COLOR if sim == 1.0 else SIMILARITY_COLOR
                badge_text = get_text("identical", self.language) if sim == 1.0 else f"{sim*100:.0f}% {get_text('similarity', self.language)}"

                badges = [get_badge(badge_text, color=badge_color)]
                if fa.size == fb.size:
                    badges.append(get_badge(format_file_size(fa.size), color=PRIMARY_COLOR))
                else:
                    badges.append(get_badge(f"{format_file_size(fa.size)} / {format_file_size(fb.size)}", color=PRIMARY_COLOR))
                if item["newer"] != "same":
                    badges.append(get_badge(get_text("cmp_newer_a" if item["newer"] == "a" else "cmp_newer_b", self.language), color=WARNING_COLOR))
                if item["larger"] != "same":
                    badges.append(get_badge(get_text("cmp_larger_a" if item["larger"] == "a" else "cmp_larger_b", self.language), color=INFO_COLOR))

                rows.append(
                    ft.Container(
                        content=ft.Row([
                            ft.Icon(ft.Icons.CHECK_CIRCLE_OUTLINE_ROUNDED if sim == 1.0 else ft.Icons.CHANGE_CIRCLE_OUTLINED, color=badge_color, size=20),
                            ft.Column([
                                ft.Text(fa.name, weight=ft.FontWeight.BOLD, size=13, color=TEXT_PRIMARY),
                                ft.Text(f"A: {format_path_short(fa.path)}", size=11, color=TEXT_MUTED),
                                ft.Text(f"B: {format_path_short(fb.path)}", size=11, color=TEXT_MUTED),
                            ], expand=True, spacing=2),
                            ft.Row(badges, spacing=6, wrap=True)
                        ], vertical_alignment=ft.CrossAxisAlignment.CENTER, spacing=10),
                        bgcolor=SURFACE_HOVER,
                        border=ft.Border.all(1, BORDER_COLOR),
                        border_radius=8,
                        padding=ft.Padding.symmetric(horizontal=12, vertical=8)
                    )
                )

        elif self.active_tab == "unique_a":
            items = self.comparison_data["unique_a"]
            if not items:
                rows.append(ft.Text(get_text("compare_no_unique_a", self.language), color=TEXT_MUTED))
            for f in items:
                rows.append(
                    ft.Container(
                        content=ft.Row([
                            ft.Icon(ft.Icons.DESCRIPTION_OUTLINED, color=INFO_COLOR, size=20),
                            ft.Column([
                                ft.Text(f.name, weight=ft.FontWeight.BOLD, size=13, color=TEXT_PRIMARY),
                                ft.Text(format_path_short(f.path), size=11, color=TEXT_MUTED),
                            ], expand=True, spacing=2),
                            get_badge(format_file_size(f.size), color=INFO_COLOR)
                        ], vertical_alignment=ft.CrossAxisAlignment.CENTER, spacing=10),
                        bgcolor=SURFACE_HOVER,
                        border=ft.Border.all(1, BORDER_COLOR),
                        border_radius=8,
                        padding=ft.Padding.symmetric(horizontal=12, vertical=8)
                    )
                )

        elif self.active_tab == "unique_b":
            items = self.comparison_data["unique_b"]
            if not items:
                rows.append(ft.Text(get_text("compare_no_unique_b", self.language), color=TEXT_MUTED))
            for f in items:
                rows.append(
                    ft.Container(
                        content=ft.Row([
                            ft.Icon(ft.Icons.DESCRIPTION_OUTLINED, color=ACCENT_COLOR, size=20),
                            ft.Column([
                                ft.Text(f.name, weight=ft.FontWeight.BOLD, size=13, color=TEXT_PRIMARY),
                                ft.Text(format_path_short(f.path), size=11, color=TEXT_MUTED),
                            ], expand=True, spacing=2),
                            get_badge(format_file_size(f.size), color=ACCENT_COLOR)
                        ], vertical_alignment=ft.CrossAxisAlignment.CENTER, spacing=10),
                        bgcolor=SURFACE_HOVER,
                        border=ft.Border.all(1, BORDER_COLOR),
                        border_radius=8,
                        padding=ft.Padding.symmetric(horizontal=12, vertical=8)
                    )
                )

        return get_styled_card(
            ft.Column(rows, spacing=8),
            padding=12
        )
