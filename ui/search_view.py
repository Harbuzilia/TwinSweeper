import flet as ft
from ui.components import (
    get_styled_card, get_primary_button, get_outlined_button, get_header_row, get_badge,
    get_mode_tile, get_drive_chip, get_folder_list_item, get_progress_card, get_detected_drives,
    PRIMARY_COLOR, ACCENT_COLOR, DANGER_COLOR, WARNING_COLOR, TEXT_PRIMARY, TEXT_SECONDARY, TEXT_MUTED, BORDER_COLOR, SURFACE_HOVER
)
from locales import get_text

class SearchView(ft.Column):
    def __init__(self, on_scan_start, language="ru", on_language_change=None):
        super().__init__()
        self.on_scan_start = on_scan_start
        self.language = language
        self.on_language_change = on_language_change

        self.selected_directories = []
        self.cancel_callback = None
        self.scroll = ft.ScrollMode.AUTO
        self.expand = True
        self.spacing = 16

        self.active_preset = "turbo"
        # Run-generation counter: guards against a cancelled scan's late
        # callbacks overwriting a newly started scan's UI (round 3).
        self._run_gen = 0

        # File Picker
        self.pick_dir_dialog = ft.FilePicker()

        # UI Controls
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

        # Perceptual Similarity Slider
        self.similarity_slider = ft.Slider(
            min=75,
            max=100,
            divisions=5,
            value=90,
            label="{value}%",
            on_change=self.on_slider_change
        )
        self.similarity_slider_label = ft.Text(
            get_text("similarity_threshold_label", self.language).format(90),
            size=13,
            weight=ft.FontWeight.BOLD,
            color=PRIMARY_COLOR
        )
        self.phash_options_container = ft.Container(
            content=ft.Column([
                self.similarity_slider_label,
                ft.Text(get_text("similarity_threshold_hint", self.language), size=12, color=TEXT_MUTED),
                self.similarity_slider
            ], spacing=6),
            visible=False,
            padding=14,
            bgcolor=SURFACE_HOVER,
            border=ft.Border.all(1, BORDER_COLOR),
            border_radius=10
        )

        # Criteria Checkboxes
        self.check_size = ft.Checkbox(label=get_text("match_size", self.language), value=True)
        self.check_hash = ft.Checkbox(label=get_text("match_hash", self.language), value=True, on_change=self._update_byte_only_warning)
        self.check_name = ft.Checkbox(label=get_text("match_name", self.language), value=False)
        self.check_byte = ft.Checkbox(label=get_text("match_byte", self.language), value=False, on_change=self._update_byte_only_warning)
        self.check_turbo = ft.Checkbox(label=get_text("turbo_mode", self.language), value=True, tooltip=get_text("turbo_hint", self.language))
        self.check_ignore_empty = ft.Checkbox(label=get_text("ignore_empty_files", self.language), value=True)

        # 2.1e: byte-only content matching (by_byte without by_hash) reads the
        # FULL content of every candidate file. After M6 the reference is read
        # once per subgroup, but the whole data volume still passes through
        # the disk — slow on large scans. Non-blocking by design: the banner
        # informs, the scan always starts.
        self.byte_only_warning = ft.Container(
            content=ft.Row([
                ft.Icon(ft.Icons.HOURGLASS_TOP_ROUNDED, color=WARNING_COLOR, size=18),
                ft.Text(get_text("byte_only_warning", self.language), size=12, color=WARNING_COLOR, expand=True)
            ], spacing=8),
            bgcolor=f"{WARNING_COLOR}18",
            border=ft.Border.all(1, f"{WARNING_COLOR}55"),
            border_radius=8,
            padding=10,
            visible=False
        )

        # Advanced Filters
        self.exclude_field = ft.TextField(
            label=get_text("exclude_filters", self.language),
            hint_text=get_text("exclude_filters_hint", self.language),
            border_color=BORDER_COLOR,
            bgcolor=SURFACE_HOVER,
            text_size=13,
            expand=True
        )

        self.min_size_field = ft.TextField(
            label=get_text("min_size_filter", self.language),
            hint_text="0",
            value="0",
            width=160,
            border_color=BORDER_COLOR,
            bgcolor=SURFACE_HOVER,
            text_size=13,
            keyboard_type=ft.KeyboardType.NUMBER
        )

        # Quick Exclusion Chips
        quick_chips = [".git", "node_modules", "venv", "AppData", "Temp", "*.log", "*.tmp"]
        chip_controls = []
        for pat in quick_chips:
            chip_controls.append(
                ft.Container(
                    content=ft.Text(f"+ {pat}", size=11, color=PRIMARY_COLOR, weight=ft.FontWeight.W_600),
                    bgcolor=f"{PRIMARY_COLOR}15",
                    border=ft.Border.all(1, f"{PRIMARY_COLOR}40"),
                    border_radius=12,
                    padding=ft.Padding.symmetric(horizontal=10, vertical=4),
                    on_click=lambda _, p=pat: self.add_quick_exclude(p),
                    ink=True
                )
            )

        self.quick_chips_row = ft.Row(
            [ft.Text(get_text("quick_exclude_label", self.language), size=12, color=TEXT_MUTED)] + chip_controls,
            wrap=True,
            spacing=6
        )

        self.advanced_visible = False
        self.advanced_container = ft.Container(
            content=ft.Column([
                ft.Text(get_text("match_criteria", self.language), size=14, weight=ft.FontWeight.W_600, color=TEXT_PRIMARY),
                ft.Row([self.check_size, self.check_hash, self.check_name, self.check_byte], wrap=True),
                ft.Divider(color=BORDER_COLOR),
                ft.Row([self.check_turbo, self.check_ignore_empty], wrap=True),
                ft.Divider(color=BORDER_COLOR),
                ft.Row([self.exclude_field, self.min_size_field]),
                self.quick_chips_row
            ], spacing=10),
            visible=False
        )

        self.toggle_advanced_btn = ft.TextButton(
            content=ft.Text(f"▼ {get_text('advanced_settings', self.language)}"),
            on_click=self.toggle_advanced,
            style=ft.ButtonStyle(color=PRIMARY_COLOR)
        )

        # Progress UI
        self.progress_bar = ft.ProgressBar(value=0, color=PRIMARY_COLOR, bgcolor=SURFACE_HOVER, visible=False)
        self.progress_text = ft.Text("", size=13, color=TEXT_SECONDARY)
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

    def build_mode_tiles(self) -> ft.ResponsiveRow:
        tiles = [
            ft.Container(
                col={"sm": 12, "md": 6, "lg": 3},
                content=get_mode_tile(
                    title=get_text("preset_turbo", self.language),
                    desc=get_text("preset_desc_turbo", self.language),
                    icon=ft.Icons.FLASH_ON_ROUNDED,
                    badge_text="⚡ Turbo",
                    is_selected=(self.active_preset == "turbo"),
                    on_click=lambda _: self.select_preset("turbo")
                )
            ),
            ft.Container(
                col={"sm": 12, "md": 6, "lg": 3},
                content=get_mode_tile(
                    title=get_text("preset_standard", self.language),
                    desc=get_text("preset_desc_standard", self.language),
                    icon=ft.Icons.SHIELD_ROUNDED,
                    badge_text="SHA-256",
                    is_selected=(self.active_preset == "standard"),
                    on_click=lambda _: self.select_preset("standard")
                )
            ),
            ft.Container(
                col={"sm": 12, "md": 6, "lg": 3},
                content=get_mode_tile(
                    title=get_text("preset_paranoid", self.language),
                    desc=get_text("preset_desc_paranoid", self.language),
                    icon=ft.Icons.VERIFIED_ROUNDED,
                    badge_text="Byte-Match",
                    is_selected=(self.active_preset == "paranoid"),
                    on_click=lambda _: self.select_preset("paranoid")
                )
            ),
            ft.Container(
                col={"sm": 12, "md": 6, "lg": 3},
                content=get_mode_tile(
                    title=get_text("similar_photos_title", self.language).split("(")[0].strip(),
                    desc=get_text("preset_desc_phash", self.language),
                    icon=ft.Icons.IMAGE_SEARCH_ROUNDED,
                    badge_text="pHash",
                    is_selected=(self.active_preset == "phash"),
                    on_click=lambda _: self.select_preset("phash")
                )
            ),
        ]
        return ft.ResponsiveRow(tiles, spacing=10)

    def select_preset(self, preset: str):
        self.active_preset = preset
        self.phash_options_container.visible = (preset == "phash")

        if preset == "turbo":
            self.check_size.value = True
            self.check_hash.value = True
            self.check_name.value = False
            self.check_byte.value = False
            self.check_turbo.value = True
        elif preset == "standard":
            self.check_size.value = True
            self.check_hash.value = True
            self.check_name.value = False
            self.check_byte.value = False
            self.check_turbo.value = False
        elif preset == "paranoid":
            self.check_size.value = True
            self.check_hash.value = True
            self.check_name.value = False
            self.check_byte.value = True
            self.check_turbo.value = False
        elif preset == "phash":
            self.check_size.value = False
            self.check_hash.value = True
            self.check_name.value = False
            self.check_byte.value = False
            self.check_turbo.value = True

        # Preset changes criteria values — resync the byte-only banner.
        self._update_byte_only_warning()

        self.build_ui()
        try:
            self.update()
        except Exception:
            pass

    def add_drive(self, drive_path: str):
        if drive_path not in self.selected_directories:
            self.selected_directories.append(drive_path)
            self.update_folders_list()

    def build_ui(self):
        # Quick Drive chips
        drive_chips = [
            get_drive_chip(
                drive_label=d,
                on_click=lambda _, drv=d: self.add_drive(drv),
                is_selected=(d in self.selected_directories)
            )
            for d in self.get_detected_drives()
        ]
        self.controls = [
            # Header
            get_header_row(
                title=get_text("find_duplicates", self.language),
                subtitle=get_text("select_folder_instruction", self.language),
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

            # Target Folders Card with Quick Drive Buttons
            get_styled_card(
                ft.Column([
                    ft.Row([
                        ft.Icon(ft.Icons.FOLDER_SPECIAL_ROUNDED, color=PRIMARY_COLOR, size=20),
                        ft.Text(get_text("selected_folders", self.language), size=15, weight=ft.FontWeight.BOLD, color=TEXT_PRIMARY),
                        get_badge(f"{len(self.selected_directories)}", color=PRIMARY_COLOR),
                        ft.Container(expand=True),
                        ft.Row([ft.Text(get_text("quick_drive_add", self.language), size=12, color=TEXT_MUTED, weight=ft.FontWeight.W_500)] + drive_chips, spacing=6, vertical_alignment=ft.CrossAxisAlignment.CENTER)
                    ]),
                    self.folders_container if self.selected_directories else self.folders_empty_hint
                ], spacing=10)
            ),

            # Presets Mode Cards
            get_styled_card(
                ft.Column([
                    ft.Row([
                        ft.Icon(ft.Icons.TUNE_ROUNDED, color=ACCENT_COLOR, size=20),
                        ft.Text(get_text("presets_title", self.language), size=15, weight=ft.FontWeight.BOLD, color=TEXT_PRIMARY),
                    ]),
                    self.build_mode_tiles(),
                    self.phash_options_container,
                    ft.Divider(color=BORDER_COLOR),
                    self.toggle_advanced_btn,
                    self.advanced_container
                ], spacing=12)
            ),

            # Byte-only slow-scan warning (2.1e) — top level so it stays
            # visible while the Advanced section is collapsed.
            self.byte_only_warning,

            # Progress & Action Card
            get_progress_card(
                status_icon=self.status_icon,
                status_text=self.progress_text,
                progress_bar=self.progress_bar,
                primary_action_btn=self.start_button,
                secondary_action_btn=self.cancel_button
            )
        ]

    def add_quick_exclude(self, pattern: str):
        current = [p.strip() for p in (self.exclude_field.value or "").split(",") if p.strip()]
        if pattern not in current:
            current.append(pattern)
            self.exclude_field.value = ", ".join(current)
            self.exclude_field.update()

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
                    badge_color=ACCENT_COLOR,
                    icon_color=PRIMARY_COLOR,
                    remove_tooltip=get_text("remove", self.language)
                )
            )
        self.build_ui()
        if self.parent:
            try:
                self.update()
            except Exception:
                pass

    def on_slider_change(self, e):
        val = int(self.similarity_slider.value)
        self.similarity_slider_label.value = get_text("similarity_threshold_label", self.language).format(val)
        self.update()

    def toggle_advanced(self, e):
        self.advanced_visible = not self.advanced_visible
        self.advanced_container.visible = self.advanced_visible
        self.toggle_advanced_btn.content = ft.Text(f"{'▲' if self.advanced_visible else '▼'} {get_text('advanced_settings', self.language)}")
        self.update()

    def _update_byte_only_warning(self, e=None):
        """Keep the byte-only banner in sync with the criteria checkboxes
        (2.1e). Non-blocking: pure visibility, never gates the scan."""
        self.byte_only_warning.visible = (
            self.check_byte.value and not self.check_hash.value
        )
        if self.parent is not None:
            try:
                self.update()
            except Exception:
                pass

    def start_scan(self, e):
        if not self.selected_directories:
            self.progress_text.value = get_text("please_select_dir", self.language)
            self.progress_text.color = DANGER_COLOR
            self.status_icon.visible = True
            self.status_icon.icon = ft.Icons.ERROR_OUTLINE_ROUNDED
            self.status_icon.color = DANGER_COLOR
            self.update()
            return

        # pHash mode matches images by content, not by these checkboxes.
        if self.active_preset != "phash" and not (
            self.check_size.value or self.check_hash.value or self.check_name.value or self.check_byte.value
        ):
            # All criteria off would otherwise treat EVERY file as one duplicate
            # group, pre-selected for deletion.
            self.progress_text.value = get_text("error_no_criteria", self.language)
            self.progress_text.color = DANGER_COLOR
            self.status_icon.visible = True
            self.status_icon.icon = ft.Icons.ERROR_OUTLINE_ROUNDED
            self.status_icon.color = DANGER_COLOR
            self.update()
            return

        # Validate min-size BEFORE touching button visibility, so an invalid
        # value can be flagged without leaving the UI stuck in "scanning".
        min_size_bytes = 0
        if self.min_size_field.value:
            try:
                min_size_bytes = int(self.min_size_field.value) * 1024
                self.min_size_field.error_text = None
                self.min_size_field.border_color = BORDER_COLOR
            except ValueError:
                # An invalid min-size must be visible, not silently treated as 0
                # (which quietly disabled the filter).
                self.min_size_field.error_text = get_text("error_invalid_number", self.language)
                self.min_size_field.border_color = DANGER_COLOR
                self.update()
                return

        self.start_button.visible = False
        self.cancel_button.visible = True
        self.progress_bar.visible = True
        self.progress_bar.value = None
        self.progress_text.value = get_text("scanning", self.language)
        self.progress_text.color = TEXT_SECONDARY
        self.status_icon.visible = True
        self.status_icon.icon = ft.Icons.HOURGLASS_TOP_ROUNDED
        self.status_icon.color = PRIMARY_COLOR
        # Resync the byte-only banner right before launch: it must be lit
        # during the scan even if the state was set programmatically (2.1e,
        # non-blocking — purely informational).
        self._update_byte_only_warning()
        self.update()

        def setup_cancel(cancel_fn):
            self.cancel_callback = cancel_fn

        exclude_patterns = []
        if self.exclude_field.value:
            exclude_patterns = [p.strip() for p in self.exclude_field.value.split(',') if p.strip()]

        is_phash = (self.active_preset == "phash")
        phash_threshold = (self.similarity_slider.value / 100.0) if is_phash else 0.90

        # Run-generation guard: if the user cancels and immediately starts a new
        # scan, the OLD worker's late callbacks must not overwrite the new
        # scan's UI (round 3 — a stale "cancelled" message reset the buttons
        # mid-scan and allowed a third parallel scan).
        self._run_gen += 1
        gen = self._run_gen

        def guarded_status(message, percent=None):
            if gen == self._run_gen:
                self.update_status(message, percent)

        def guarded_finished():
            if gen == self._run_gen:
                self.on_scan_finished()

        self.on_scan_start(
            directories=self.selected_directories,
            by_name=self.check_name.value,
            by_size=self.check_size.value,
            by_hash=self.check_hash.value,
            by_byte=self.check_byte.value,
            progress_callback=guarded_status,
            on_cancel_setup=setup_cancel,
            exclude_patterns=exclude_patterns,
            turbo_mode=self.check_turbo.value,
            min_size_bytes=min_size_bytes,
            ignore_empty_files=self.check_ignore_empty.value,
            on_scan_finished=guarded_finished,
            is_phash=is_phash,
            phash_threshold=phash_threshold
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
        self.progress_text.value = message
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
        self.progress_text.value = get_text("scan_cancelled", self.language)
        self.progress_bar.visible = False
        self.start_button.visible = True
        self.cancel_button.visible = False
        self.status_icon.visible = False
        self.update()

    def reset_state(self):
        self.start_button.visible = True
        self.cancel_button.visible = False
        self.progress_bar.visible = False
        self.progress_text.value = ""
        self.status_icon.visible = False
        if self.parent:
            try:
                self.update()
            except Exception:
                pass
