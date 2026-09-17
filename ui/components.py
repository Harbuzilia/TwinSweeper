import flet as ft
import os
import sys
import string
from typing import Optional, Callable, Dict, List, Tuple

THEMES: Dict[str, Dict[str, str]] = {
    "dark_slate": {
        "BG_COLOR": "#090D16",
        "SURFACE_COLOR": "#111827",
        "SURFACE_CARD": "#162032",
        "SURFACE_HOVER": "#1E293B",
        "BORDER_COLOR": "#1F2937",
        "BORDER_LIGHT": "#334155",
        "PRIMARY_COLOR": "#6366F1",
        "PRIMARY_LIGHT": "#818CF8",
        "PRIMARY_HOVER": "#4F46E5",
        "ACCENT_COLOR": "#8B5CF6",
        "SUCCESS_COLOR": "#10B981",
        "WARNING_COLOR": "#F59E0B",
        "DANGER_COLOR": "#EF4444",
        "INFO_COLOR": "#06B6D4",
        "TEXT_PRIMARY": "#F8FAFC",
        "TEXT_SECONDARY": "#94A3B8",
        "TEXT_MUTED": "#64748B",
        "SHADOW_COLOR": "#0000003D",
        "SIMILARITY_COLOR": "#8B5CF6",
    },
    "midnight_oled": {
        "BG_COLOR": "#000000",
        "SURFACE_COLOR": "#0A0A0C",
        "SURFACE_CARD": "#121215",
        "SURFACE_HOVER": "#1C1C22",
        "BORDER_COLOR": "#27272A",
        "BORDER_LIGHT": "#3F3F46",
        "PRIMARY_COLOR": "#3B82F6",
        "PRIMARY_LIGHT": "#60A5FA",
        "PRIMARY_HOVER": "#2563EB",
        "ACCENT_COLOR": "#A855F7",
        "SUCCESS_COLOR": "#10B981",
        "WARNING_COLOR": "#F59E0B",
        "DANGER_COLOR": "#EF4444",
        "INFO_COLOR": "#06B6D4",
        "TEXT_PRIMARY": "#FAFAFA",
        "TEXT_SECONDARY": "#A1A1AA",
        "TEXT_MUTED": "#71717A",
        "SHADOW_COLOR": "#0000005C",
        "SIMILARITY_COLOR": "#8B5CF6",
    },
    "light_clean": {
        "BG_COLOR": "#F8FAFC",
        "SURFACE_COLOR": "#FFFFFF",
        "SURFACE_CARD": "#FFFFFF",
        "SURFACE_HOVER": "#F1F5F9",
        "BORDER_COLOR": "#E2E8F0",
        "BORDER_LIGHT": "#CBD5E1",
        "PRIMARY_COLOR": "#4F46E5",
        "PRIMARY_LIGHT": "#6366F1",
        "PRIMARY_HOVER": "#4338CA",
        "ACCENT_COLOR": "#7C3AED",
        "SUCCESS_COLOR": "#059669",
        "WARNING_COLOR": "#D97706",
        "DANGER_COLOR": "#DC2626",
        "INFO_COLOR": "#0284C7",
        "TEXT_PRIMARY": "#0F172A",
        "TEXT_SECONDARY": "#475569",
        "TEXT_MUTED": "#94A3B8",
        "SHADOW_COLOR": "#0F172A14",
        "SIMILARITY_COLOR": "#8B5CF6",
    }
}

ACTIVE_THEME_KEY = "dark_slate"

_PALETTE_KEYS = tuple(THEMES["dark_slate"].keys())

def get_current_theme() -> Dict[str, str]:
    return THEMES.get(ACTIVE_THEME_KEY, THEMES["dark_slate"])

def get_active_theme_key() -> str:
    return ACTIVE_THEME_KEY

def set_active_theme(theme_name: str):
    """Switches the active palette and propagates new colors everywhere."""
    global ACTIVE_THEME_KEY
    if theme_name not in THEMES or theme_name == ACTIVE_THEME_KEY:
        return
    old_palette = THEMES.get(ACTIVE_THEME_KEY, THEMES["dark_slate"])
    new_palette = THEMES[theme_name]
    ACTIVE_THEME_KEY = theme_name

    this_module = sys.modules[__name__]
    for key, value in new_palette.items():
        setattr(this_module, key, value)

    for module in list(sys.modules.values()):
        if module is None or module is this_module:
            continue
        mod_dict = getattr(module, "__dict__", None)
        if not mod_dict:
            continue
        for key in _PALETTE_KEYS:
            if mod_dict.get(key) == old_palette.get(key):
                mod_dict[key] = new_palette[key]

# Default palette aliases
BG_COLOR = THEMES["dark_slate"]["BG_COLOR"]
SURFACE_COLOR = THEMES["dark_slate"]["SURFACE_COLOR"]
SURFACE_CARD = THEMES["dark_slate"]["SURFACE_CARD"]
SURFACE_HOVER = THEMES["dark_slate"]["SURFACE_HOVER"]
BORDER_COLOR = THEMES["dark_slate"]["BORDER_COLOR"]
BORDER_LIGHT = THEMES["dark_slate"]["BORDER_LIGHT"]
PRIMARY_COLOR = THEMES["dark_slate"]["PRIMARY_COLOR"]
PRIMARY_LIGHT = THEMES["dark_slate"]["PRIMARY_LIGHT"]
ACCENT_COLOR = THEMES["dark_slate"]["ACCENT_COLOR"]
SUCCESS_COLOR = THEMES["dark_slate"]["SUCCESS_COLOR"]
WARNING_COLOR = THEMES["dark_slate"]["WARNING_COLOR"]
DANGER_COLOR = THEMES["dark_slate"]["DANGER_COLOR"]
INFO_COLOR = THEMES["dark_slate"]["INFO_COLOR"]
SIMILARITY_COLOR = THEMES["dark_slate"]["SIMILARITY_COLOR"]
TEXT_PRIMARY = THEMES["dark_slate"]["TEXT_PRIMARY"]
TEXT_SECONDARY = THEMES["dark_slate"]["TEXT_SECONDARY"]
TEXT_MUTED = THEMES["dark_slate"]["TEXT_MUTED"]

CATEGORY_ICONS = {
    "images": (ft.Icons.IMAGE_OUTLINED, "#38BDF8"),
    "videos": (ft.Icons.VIDEOCAM_OUTLINED, "#A855F7"),
    "audio": (ft.Icons.AUDIOTRACK_OUTLINED, "#EC4899"),
    "documents": (ft.Icons.DESCRIPTION_OUTLINED, "#F59E0B"),
    "archives": (ft.Icons.FOLDER_ZIP_OUTLINED, "#F97316"),
    "code": (ft.Icons.CODE_ROUNDED, "#10B981"),
    "other": (ft.Icons.INSERT_DRIVE_FILE_OUTLINED, "#94A3B8"),
}

def format_path_short(path: str, max_chars: int = 55) -> str:
    """Smartly truncates path in the middle: C:\\Users\\...\\Subdir\\file.txt"""
    if len(path) <= max_chars:
        return path
    drive, rest = os.path.splitdrive(path)
    parts = rest.strip("\\/").split(os.sep)
    if len(parts) <= 2:
        return path[:max_chars // 2] + "..." + path[-(max_chars // 2):]
    first = parts[0]
    last = parts[-1]
    return f"{drive}\\{first}\\...\\{last}"


def get_detected_drives() -> List[str]:
    """Return a list of existing Windows drive letters (e.g. ['C:\\', 'D:\\'])."""
    return [f"{d}:\\" for d in string.ascii_uppercase if os.path.exists(f"{d}:\\")]


def get_styled_card(
    content: ft.Control,
    padding: int = 18,
    border_radius: int = 12,
    border_color: Optional[str] = None,
    bgcolor: Optional[str] = None,
    expand: bool = False,
    shadow: bool = True
) -> ft.Container:
    theme = get_current_theme()
    card_shadow = (
        ft.BoxShadow(
            blur_radius=12,
            spread_radius=0,
            color=theme.get("SHADOW_COLOR", "#0000002E"),
            offset=ft.Offset(0, 4)
        )
        if shadow
        else None
    )
    return ft.Container(
        content=content,
        bgcolor=bgcolor or theme["SURFACE_CARD"],
        border_radius=border_radius,
        padding=padding,
        border=ft.Border.all(1, border_color or theme["BORDER_COLOR"]),
        shadow=card_shadow,
        expand=expand
    )

def get_stat_card(
    title: str,
    value: str,
    subtitle: str = "",
    icon: str = ft.Icons.DATA_SAVER_OFF_ROUNDED,
    icon_color: Optional[str] = None,
    expand: bool = True
) -> ft.Container:
    theme = get_current_theme()
    ic_col = icon_color or theme["PRIMARY_COLOR"]
    return ft.Container(
        content=ft.Row([
            ft.Container(
                content=ft.Icon(icon, color=ic_col, size=24),
                bgcolor=f"{ic_col}1F",
                border_radius=10,
                padding=12,
            ),
            ft.Column([
                ft.Text(title, size=12, color=theme["TEXT_SECONDARY"], weight=ft.FontWeight.W_500),
                ft.Text(value, size=22, color=theme["TEXT_PRIMARY"], weight=ft.FontWeight.BOLD),
                ft.Text(subtitle, size=11, color=theme["TEXT_MUTED"]) if subtitle else ft.Container(),
            ], spacing=2, expand=True),
        ], alignment=ft.MainAxisAlignment.START, vertical_alignment=ft.CrossAxisAlignment.CENTER),
        bgcolor=theme["SURFACE_CARD"],
        border_radius=12,
        padding=16,
        border=ft.Border.all(1, theme["BORDER_COLOR"]),
        shadow=ft.BoxShadow(
            blur_radius=10,
            spread_radius=0,
            color=theme.get("SHADOW_COLOR", "#00000028"),
            offset=ft.Offset(0, 3)
        ),
        expand=expand
    )

def get_primary_button(
    text: str,
    on_click: Optional[Callable] = None,
    icon: Optional[str] = None,
    bgcolor: Optional[str] = None,
    color: str = "#FFFFFF",
    height: int = 42,
    expand: bool = False
) -> ft.Button:
    theme = get_current_theme()
    bg = bgcolor or theme["PRIMARY_COLOR"]
    hover_bg = theme.get("PRIMARY_HOVER", bg) if not bgcolor else bg
    return ft.Button(
        content=ft.Row([
            ft.Icon(icon, size=18, color=color) if icon else ft.Container(),
            ft.Text(text, color=color, weight=ft.FontWeight.W_600, size=13)
        ], spacing=7, alignment=ft.MainAxisAlignment.CENTER),
        on_click=on_click,
        style=ft.ButtonStyle(
            bgcolor={
                ft.ControlState.HOVERED: hover_bg,
                ft.ControlState.DEFAULT: bg,
            },
            shape=ft.RoundedRectangleBorder(radius=8),
            padding=ft.Padding.symmetric(horizontal=16, vertical=10),
            elevation={
                ft.ControlState.HOVERED: 3,
                ft.ControlState.DEFAULT: 1,
            },
            animation_duration=150,
        ),
        height=height,
        expand=expand
    )

def get_outlined_button(
    text: str,
    on_click: Optional[Callable] = None,
    icon: Optional[str] = None,
    border_color: Optional[str] = None,
    color: Optional[str] = None,
    height: int = 40,
    expand: bool = False
) -> ft.Button:
    theme = get_current_theme()
    b_col = border_color or theme["BORDER_LIGHT"]
    t_col = color or theme["TEXT_PRIMARY"]
    return ft.Button(
        content=ft.Row([
            ft.Icon(icon, size=16, color=t_col) if icon else ft.Container(),
            ft.Text(text, color=t_col, weight=ft.FontWeight.W_500, size=13)
        ], spacing=6, alignment=ft.MainAxisAlignment.CENTER),
        on_click=on_click,
        style=ft.ButtonStyle(
            bgcolor={
                ft.ControlState.HOVERED: theme["SURFACE_HOVER"],
                ft.ControlState.DEFAULT: "transparent",
            },
            side={
                ft.ControlState.HOVERED: ft.BorderSide(1.5, theme["PRIMARY_COLOR"]),
                ft.ControlState.DEFAULT: ft.BorderSide(1, b_col),
            },
            shape=ft.RoundedRectangleBorder(radius=8),
            padding=ft.Padding.symmetric(horizontal=14, vertical=8),
            elevation=0,
            animation_duration=150,
        ),
        height=height,
        expand=expand
    )
def get_badge(text: str, color: Optional[str] = None, icon: Optional[str] = None) -> ft.Container:
    theme = get_current_theme()
    c = color or theme["PRIMARY_COLOR"]
    content_list = []
    if icon:
        content_list.append(ft.Icon(icon, size=12, color=c))
    content_list.append(ft.Text(text, size=11, color=c, weight=ft.FontWeight.W_600))

    return ft.Container(
        content=ft.Row(content_list, spacing=4, alignment=ft.MainAxisAlignment.CENTER),
        bgcolor=f"{c}22",
        border_radius=12,
        padding=ft.Padding.symmetric(horizontal=8, vertical=3),
    )

def get_header_row(title: str, subtitle: Optional[str] = None, action_control: Optional[ft.Control] = None) -> ft.Row:
    theme = get_current_theme()
    text_col = ft.Column([
        ft.Text(title, size=22, weight=ft.FontWeight.BOLD, color=theme["TEXT_PRIMARY"]),
        ft.Text(subtitle, size=13, color=theme["TEXT_SECONDARY"]) if subtitle else ft.Container(),
    ], spacing=2)

    controls = [text_col]
    if action_control:
        return ft.Row([text_col, action_control], alignment=ft.MainAxisAlignment.SPACE_BETWEEN)
    return ft.Row(controls)

def get_kpi_badge(icon: str, label: str, value: str, color: Optional[str] = None) -> ft.Container:
    """Compact high-density metric badge for master status strips."""
    theme = get_current_theme()
    accent = color or theme["PRIMARY_COLOR"]
    return ft.Container(
        content=ft.Row([
            ft.Icon(icon, size=16, color=accent),
            ft.Text(label, size=11, color=theme["TEXT_MUTED"]),
            ft.Text(value, size=12, weight=ft.FontWeight.BOLD, color=theme["TEXT_PRIMARY"]),
        ], spacing=6, alignment=ft.MainAxisAlignment.CENTER),
        bgcolor=theme["SURFACE_HOVER"],
        border=ft.Border.all(1, theme["BORDER_COLOR"]),
        border_radius=8,
        padding=ft.Padding.symmetric(horizontal=12, vertical=6)
    )

def bind_hover_effect(container: ft.Container, normal_bg: str, hover_bg: str):
    """Binds hover enter/leave events to toggle a container's background colour."""
    def _on_hover(e):
        e.control.bgcolor = hover_bg if e.data == "true" else normal_bg
        try:
            e.control.update()
        except Exception:
            pass
    container.on_hover = _on_hover


def get_mode_tile(
    title: str,
    desc: str,
    icon: str,
    badge_text: Optional[str] = None,
    is_selected: bool = False,
    on_click: Optional[Callable] = None
) -> ft.Container:
    """Interactive selectable mode tile with clean visual feedback."""
    theme = get_current_theme()
    accent = theme["PRIMARY_COLOR"] if is_selected else theme["TEXT_MUTED"]
    bg = f"{theme['PRIMARY_COLOR']}1C" if is_selected else theme["SURFACE_CARD"]
    border_col = theme["PRIMARY_COLOR"] if is_selected else theme["BORDER_COLOR"]

    header_row = [
        ft.Container(
            content=ft.Icon(icon, size=20, color=accent),
            bgcolor=f"{accent}22" if is_selected else theme["SURFACE_HOVER"],
            border_radius=8,
            padding=6,
        ),
        ft.Text(
            title,
            size=14,
            weight=ft.FontWeight.BOLD if is_selected else ft.FontWeight.W_600,
            color=theme["TEXT_PRIMARY"],
            expand=True
        )
    ]
    if badge_text:
        header_row.append(get_badge(badge_text, color=theme["PRIMARY_COLOR"] if is_selected else theme["TEXT_MUTED"]))

    tile_shadow = (
        ft.BoxShadow(
            blur_radius=8,
            spread_radius=0,
            color=f"{theme['PRIMARY_COLOR']}2B",
            offset=ft.Offset(0, 2)
        )
        if is_selected
        else ft.BoxShadow(
            blur_radius=4,
            spread_radius=0,
            color=theme.get("SHADOW_COLOR", "#0000001A"),
            offset=ft.Offset(0, 1)
        )
    )

    return ft.Container(
        content=ft.Column([
            ft.Row(header_row, alignment=ft.MainAxisAlignment.SPACE_BETWEEN, vertical_alignment=ft.CrossAxisAlignment.CENTER, spacing=10),
            ft.Text(desc, size=12, color=theme["TEXT_SECONDARY"] if is_selected else theme["TEXT_MUTED"], max_lines=2),
        ], spacing=8),
        bgcolor=bg,
        border=ft.Border.all(2 if is_selected else 1, border_col),
        border_radius=12,
        padding=14,
        shadow=tile_shadow,
        on_click=on_click,
        ink=True
    )

def get_segmented_control(
    options: List[Tuple[str, str, Optional[str]]],
    selected_key: str,
    on_select: Callable[[str], None]
) -> ft.Container:
    """Sleek segmented pill tab bar."""
    theme = get_current_theme()
    tabs = []
    for key, label, icon in options:
        is_active = (key == selected_key)
        tabs.append(
            ft.Container(
                content=ft.Row([
                    ft.Icon(icon, size=15, color="#FFFFFF" if is_active else theme["TEXT_SECONDARY"]) if icon else ft.Container(),
                    ft.Text(label, size=12, weight=ft.FontWeight.W_600 if is_active else ft.FontWeight.NORMAL, color="#FFFFFF" if is_active else theme["TEXT_SECONDARY"])
                ], spacing=6, alignment=ft.MainAxisAlignment.CENTER),
                bgcolor=theme["PRIMARY_COLOR"] if is_active else "transparent",
                border_radius=7,
                padding=ft.Padding.symmetric(horizontal=14, vertical=7),
                shadow=ft.BoxShadow(blur_radius=6, spread_radius=0, color=f"{theme['PRIMARY_COLOR']}3D", offset=ft.Offset(0, 2)) if is_active else None,
                on_click=lambda _, k=key: on_select(k),
                ink=True
            )
        )
    return ft.Container(
        content=ft.Row(tabs, spacing=4, tight=True),
        bgcolor=theme["SURFACE_CARD"],
        border=ft.Border.all(1, theme["BORDER_COLOR"]),
        border_radius=9,
        padding=4
    )

def get_drive_chip(
    drive_label: str,
    on_click: Optional[Callable] = None,
    is_selected: bool = False,
    accent_color: Optional[str] = None,
    tooltip: Optional[str] = None,
) -> ft.Container:
    """
    Standardized tactile quick-drive chip with storage icon,
    badge style, hover elevation, and optional selected state.
    """
    theme = get_current_theme()
    eff_accent = accent_color or theme["PRIMARY_COLOR"]
    bg = f"{eff_accent}1C" if is_selected else theme["SURFACE_CARD"]
    border_col = eff_accent if is_selected else theme["BORDER_COLOR"]
    text_col = eff_accent if is_selected else theme["TEXT_PRIMARY"]
    icon_col = eff_accent if is_selected else theme["TEXT_SECONDARY"]
    eff_tooltip = tooltip if tooltip is not None else drive_label

    return ft.Container(
        content=ft.Row([
            ft.Icon(ft.Icons.STORAGE_ROUNDED, size=13, color=icon_col),
            ft.Text(drive_label, size=12, weight=ft.FontWeight.W_600, color=text_col),
        ], spacing=5, alignment=ft.MainAxisAlignment.CENTER),
        bgcolor=bg,
        border=ft.Border.all(1, border_col),
        border_radius=7,
        padding=ft.Padding.symmetric(horizontal=9, vertical=5),
        on_click=on_click,
        ink=True,
        tooltip=eff_tooltip,
    )


def get_progress_card(
    status_icon: ft.Control,
    status_text: ft.Control,
    progress_bar: ft.Control,
    primary_action_btn: Optional[ft.Control] = None,
    secondary_action_btn: Optional[ft.Control] = None,
    meta_text: Optional[ft.Control] = None,
) -> ft.Container:
    """
    Unified modern scanning/task progress card with status indicator,
    progress bar, meta information, and centered action buttons.
    """
    actions = []
    if primary_action_btn:
        actions.append(primary_action_btn)
    if secondary_action_btn:
        actions.append(secondary_action_btn)

    header_row = [status_icon, status_text]
    if meta_text:
        header_row.extend([ft.Container(expand=True), meta_text])

    return get_styled_card(
        ft.Column([
            ft.Row(header_row, alignment=ft.MainAxisAlignment.CENTER if not meta_text else ft.MainAxisAlignment.START, vertical_alignment=ft.CrossAxisAlignment.CENTER),
            progress_bar,
            ft.Row(actions, alignment=ft.MainAxisAlignment.CENTER, spacing=12) if actions else ft.Container(),
        ], spacing=12)
    )


def get_action_icon_button(
    icon: str,
    tooltip: str = "",
    on_click: Optional[Callable] = None,
    icon_color: Optional[str] = None,
    icon_size: int = 18,
    button_size: int = 34,
) -> ft.Container:
    """
    Modern micro-interaction action icon button with smooth hover effect,
    subtle border and rounded aesthetics.
    """
    theme = get_current_theme()
    eff_icon_color = icon_color or theme["TEXT_SECONDARY"]

    btn_icon = ft.Icon(icon, size=icon_size, color=eff_icon_color)

    container = ft.Container(
        content=btn_icon,
        width=button_size,
        height=button_size,
        alignment=ft.Alignment(0, 0),
        border_radius=8,
        border=ft.Border.all(1, "transparent"),
        bgcolor="transparent",
        on_click=on_click,
        ink=True,
        tooltip=tooltip
    )
    bind_hover_effect(container, "transparent", theme["SURFACE_HOVER"])
    # Extend hover to also change icon colour
    orig_hover = container.on_hover
    def combined_hover(e):
        orig_hover(e)
        btn_icon.color = theme["PRIMARY_COLOR"] if e.data == "true" else eff_icon_color
        try:
            e.control.update()
        except Exception:
            pass
    container.on_hover = combined_hover
    return container


def get_styled_dialog(
    title: str,
    content: Optional[ft.Control] = None,
    actions: Optional[List[ft.Control]] = None,
    icon: Optional[str] = None,
    icon_color: Optional[str] = None,
    title_color: Optional[str] = None,
    width: Optional[int] = None,
) -> ft.AlertDialog:
    """
    Modern themed modal dialog with header icon badge, proper typography,
    accent border, and surface styling.
    """
    theme = get_current_theme()
    eff_title_col = title_color or theme["TEXT_PRIMARY"]
    eff_icon_col = icon_color or theme["PRIMARY_COLOR"]

    title_items = []
    if icon:
        title_items.append(
            ft.Container(
                content=ft.Icon(icon, size=18, color=eff_icon_col),
                bgcolor=f"{eff_icon_col}20",
                padding=6,
                border_radius=8
            )
        )
    title_items.append(ft.Text(title, size=16, weight=ft.FontWeight.W_600, color=eff_title_col))

    title_widget = ft.Row(title_items, spacing=10, vertical_alignment=ft.CrossAxisAlignment.CENTER)

    # content is optional: a title-only dialog (e.g. a confirmation toast)
    # must not crash the caller — round 3 found two settings handlers passing
    # no content and dying with a TypeError after the real work was done.
    body = content if content is not None else ft.Container()
    wrapped_content = body
    if width:
        wrapped_content = ft.Container(content=body, width=width)

    return ft.AlertDialog(
        title=title_widget,
        content=wrapped_content,
        actions=actions or [],
        bgcolor=theme["SURFACE_CARD"],
        shape=ft.RoundedRectangleBorder(radius=12),
        actions_padding=ft.Padding.only(right=16, bottom=16, left=16),
        content_padding=ft.Padding.only(top=12, right=20, bottom=16, left=20),
        title_padding=ft.Padding.only(top=16, right=20, bottom=8, left=20)
    )


def get_folder_list_item(
    path: str,
    on_remove,
    badge_color: Optional[str] = None,
    icon_color: Optional[str] = None,
    remove_tooltip: str = "Remove"
) -> ft.Container:
    """
    Modern styled folder list entry with drive badge, short path representation,
    and action icon button for deletion.
    """
    theme = get_current_theme()
    drive_letter = os.path.splitdrive(path)[0]
    short_p = format_path_short(path, max_chars=60)
    ic_col = icon_color or theme["PRIMARY_COLOR"]
    bdg_col = badge_color or theme["PRIMARY_COLOR"]

    row_controls = [
        ft.Icon(ft.Icons.FOLDER_ROUNDED, color=ic_col, size=18),
    ]
    if drive_letter:
        row_controls.append(get_badge(drive_letter, color=bdg_col))

    row_controls.append(
        ft.Text(short_p, size=13, color=theme["TEXT_PRIMARY"], expand=True, tooltip=path, weight=ft.FontWeight.W_500)
    )
    row_controls.append(
        get_action_icon_button(
            icon=ft.Icons.CLOSE_ROUNDED,
            on_click=on_remove,
            tooltip=remove_tooltip,
            icon_color=theme["TEXT_MUTED"],
            button_size=28,
            icon_size=16
        )
    )

    return ft.Container(
        content=ft.Row(row_controls, spacing=10, vertical_alignment=ft.CrossAxisAlignment.CENTER),
        bgcolor=theme["SURFACE_HOVER"],
        border=ft.Border.all(1, theme["BORDER_COLOR"]),
        border_radius=8,
        padding=ft.Padding.symmetric(horizontal=12, vertical=8)
    )
