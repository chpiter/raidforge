"""Two palettes: the default 7.2 deep-blue theme and an explicit mono option.

All colours live here so TCSS, Rich `Text` styles and the Textual `Theme`
objects cannot drift apart.  The stylesheet itself only references variables;
switching to ``raidforge-mono`` turns every variable black or white.  The
mono palette is never selected automatically from ``TERM``.
"""

from __future__ import annotations

import os
from typing import Dict, Optional

from rich.style import Style

from .models import ICON_BAD, ICON_IDLE, ICON_OK, ICON_RUNNING, ICON_SELECTED, ICON_WARNING, StatusKind

#: 7.2 palette from the design document.
BLUE_BG = "#002B49"
BLUE_SURFACE = "#013A5E"
BLUE_PANEL = "#01456E"
BLUE_FG = "#E6F4FA"
BLUE_ACCENT = "#4FC3F7"
BLUE_MUTED = "#8FB3C7"
BLUE_SUCCESS = "#66BB6A"
BLUE_WARNING = "#FFB74D"
BLUE_DANGER = "#FF5252"
BLUE_BORDER = "#1E6A96"
BLUE_ZEBRA = "#013451"
BLUE_NAV_ACTIVE = "#0066A1"
BLUE_NAV_ACTIVE_FG = "#FFFFFF"
#: High-contrast focus colour, deliberately distinct from the cyan accent.
BLUE_FOCUS = "#FFC107"

#: The shipped 16-colour console only trusts pure black and white.
MONO_BG = "#000000"
MONO_FG = "#FFFFFF"

THEME_BLUE = "raidforge"
THEME_MONO = "raidforge-mono"

#: Variables that must resolve to the background colour in the mono theme.
_BG_VARS = (
    "surface",
    "panel",
    "boost",
    "secondary-muted",
    "block-hover-background",
    "block-cursor-blurred-background",
    "input-selection-background",
    "scrollbar-background",
    "scrollbar-background-hover",
    "scrollbar-background-active",
    "scrollbar-corner-color",
    "footer-background",
    "footer-key-background",
    "footer-description-background",
    "footer-item-background",
    "link-background",
    "link-background-hover",
    "surface-darken-1",
    "surface-lighten-1",
    "surface-lighten-2",
    "surface-lighten-3",
    "panel-darken-1",
    "panel-darken-2",
    "panel-lighten-1",
    "boost-darken-1",
    "boost-lighten-1",
    "primary-darken-2",
    "primary-darken-3",
    "accent-darken-1",
    "error-darken-1",
    "error-darken-2",
    "error-darken-3",
    "success-darken-2",
    "success-darken-3",
    "warning-darken-1",
    "warning-darken-2",
    "warning-darken-3",
)

#: Variables that must resolve to the foreground colour in the mono theme.
_FG_VARS = (
    "foreground",
    "text",
    "primary",
    "secondary",
    "accent",
    "success",
    "warning",
    "error",
    "text-muted",
    "text-disabled",
    "text-primary",
    "text-secondary",
    "text-accent",
    "text-success",
    "text-warning",
    "text-error",
    "primary-muted",
    "accent-muted",
    "success-muted",
    "warning-muted",
    "error-muted",
    "primary-lighten-3",
    "accent-lighten-1",
    "error-lighten-2",
    "success-lighten-1",
    "success-lighten-2",
    "warning-lighten-2",
    "border",
    "border-blurred",
    "block-cursor-background",
    "block-cursor-blurred-foreground",
    "input-cursor-background",
    "button-foreground",
    "button-color-foreground",
    "link-color",
    "link-color-hover",
    "scrollbar",
    "scrollbar-hover",
    "scrollbar-active",
    "footer-foreground",
    "footer-key-foreground",
    "footer-description-foreground",
)

_STYLE_VARS = {
    "block-cursor-text-style": "bold",
    "block-cursor-blurred-text-style": "bold",
    "input-cursor-text-style": "bold",
    "button-focus-text-style": "bold",
}

#: Application-specific variables referenced by app.tcss.
_APP_VARS_BLUE = {
    "focus": BLUE_FOCUS,
    "focus-foreground": BLUE_BG,
    "focus-blurred": BLUE_SURFACE,
    "block-cursor-background": BLUE_FOCUS,
    "block-cursor-foreground": BLUE_BG,
    "block-cursor-blurred-background": BLUE_SURFACE,
    "block-cursor-blurred-foreground": BLUE_FG,
    "input-cursor-background": BLUE_BG,
    "nav-bg": BLUE_PANEL,
    "nav-active-bg": BLUE_NAV_ACTIVE,
    "nav-active-fg": BLUE_NAV_ACTIVE_FG,
    "nav-tab-border": BLUE_BORDER,
    "nav-hover-bg": BLUE_SURFACE,
    "table-zebra": BLUE_ZEBRA,
    "card-server": BLUE_ACCENT,
    "card-controllers": BLUE_SUCCESS,
    "card-storage": BLUE_WARNING,
    "card-health": BLUE_DANGER,
    "drawer-bg": BLUE_SURFACE,
    "hotkey-danger": BLUE_DANGER,
    "status-ok": BLUE_SUCCESS,
    "status-warn": BLUE_WARNING,
    "status-bad": BLUE_DANGER,
    "status-idle": BLUE_MUTED,
    "status-running": BLUE_ACCENT,
}

_APP_VARS_MONO = {
    "focus": MONO_FG,
    "focus-foreground": MONO_BG,
    "focus-blurred": MONO_BG,
    "block-cursor-background": MONO_FG,
    "block-cursor-foreground": MONO_BG,
    "block-cursor-blurred-background": MONO_BG,
    "block-cursor-blurred-foreground": MONO_FG,
    "input-cursor-background": MONO_BG,
    "nav-bg": MONO_BG,
    "nav-active-bg": MONO_FG,
    "nav-active-fg": MONO_BG,
    "nav-tab-border": MONO_FG,
    "nav-hover-bg": MONO_BG,
    "table-zebra": MONO_BG,
    "card-server": MONO_FG,
    "card-controllers": MONO_FG,
    "card-storage": MONO_FG,
    "card-health": MONO_FG,
    "drawer-bg": MONO_BG,
    "hotkey-danger": MONO_FG,
    "status-ok": MONO_FG,
    "status-warn": MONO_FG,
    "status-bad": MONO_FG,
    "status-idle": MONO_FG,
    "status-running": MONO_FG,
}


def _theme(name: str, colours: Dict[str, str], app_vars: Dict[str, str]):
    from textual.theme import Theme

    variables = {var: colours["background"] for var in _BG_VARS}
    variables.update({var: colours["foreground"] for var in _FG_VARS})
    variables.update(_STYLE_VARS)
    variables.update(app_vars)
    variables.update(
        {
            "surface": colours["surface"],
            "panel": colours["panel"],
            "border": colours["border"],
            "text-muted": colours["muted"],
            "secondary": colours["muted"],
        }
    )
    return Theme(
        name=name,
        primary=colours["accent"],
        secondary=colours["muted"],
        accent=colours["accent"],
        warning=colours["warning"],
        error=colours["danger"],
        success=colours["success"],
        foreground=colours["foreground"],
        background=colours["background"],
        surface=colours["surface"],
        panel=colours["panel"],
        boost=colours["panel"],
        dark=True,
        luminosity_spread=0.0,
        text_alpha=1.0,
        variables=variables,
    )


def raidforge_theme():
    """The 7.2 deep-blue theme."""

    return _theme(
        THEME_BLUE,
        {
            "background": BLUE_BG,
            "surface": BLUE_SURFACE,
            "panel": BLUE_PANEL,
            "foreground": BLUE_FG,
            "accent": BLUE_ACCENT,
            "muted": BLUE_MUTED,
            "success": BLUE_SUCCESS,
            "warning": BLUE_WARNING,
            "danger": BLUE_DANGER,
            "border": BLUE_BORDER,
        },
        _APP_VARS_BLUE,
    )


def mono_theme():
    """The 16-colour console fallback: pure black and white only."""

    return _theme(
        THEME_MONO,
        {
            "background": MONO_BG,
            "surface": MONO_BG,
            "panel": MONO_BG,
            "foreground": MONO_FG,
            "accent": MONO_FG,
            "muted": MONO_FG,
            "success": MONO_FG,
            "warning": MONO_FG,
            "danger": MONO_FG,
            "border": MONO_FG,
        },
        _APP_VARS_MONO,
    )


def select_theme_name(configured: str = "auto", env: Optional[Dict[str, str]] = None) -> str:
    """Pick a theme from config or an explicit override.

    The deep-blue palette is the default on every terminal.  ``auto`` is kept
    as a backwards-compatible alias for blue; monochrome is only selected when
    the operator asks for it explicitly (config, ``RAIDFORGE_THEME=mono`` or
    ``RAIDFORGE_MONO=1``).
    """

    env = dict(os.environ if env is None else env)
    override = (env.get("RAIDFORGE_THEME") or "").strip().lower()
    choice = (configured or "auto").strip().lower()
    if override in ("mono", "black", "white", THEME_MONO):
        return THEME_MONO
    if override in ("blue", THEME_BLUE):
        return THEME_BLUE
    if choice in ("mono", "black", "white", THEME_MONO):
        return THEME_MONO
    if choice in ("blue", THEME_BLUE):
        return THEME_BLUE
    if env.get("RAIDFORGE_MONO") == "1":
        return THEME_MONO
    return THEME_BLUE


def focus_colour(theme_name: str = THEME_BLUE) -> str:
    """High-contrast focus colour; mono stays pure white."""

    return MONO_FG if theme_name == THEME_MONO else BLUE_FOCUS


_STATUS_COLOURS_BLUE = {
    StatusKind.OK: BLUE_SUCCESS,
    StatusKind.WARN: BLUE_WARNING,
    StatusKind.BAD: BLUE_DANGER,
    StatusKind.IDLE: BLUE_MUTED,
    StatusKind.RUNNING: BLUE_ACCENT,
    StatusKind.SELECTED: BLUE_ACCENT,
}

_STATUS_COLOURS_MONO = {kind: MONO_FG for kind in StatusKind}


def status_style(kind: StatusKind, theme_name: str = THEME_BLUE) -> Style:
    """Rich style for a status icon; the mono theme never introduces a colour."""

    colour = (_STATUS_COLOURS_MONO if theme_name == THEME_MONO else _STATUS_COLOURS_BLUE)[kind]
    return Style(color=colour)


def status_icon(kind: StatusKind) -> str:
    return {
        StatusKind.OK: ICON_OK,
        StatusKind.WARN: ICON_WARNING,
        StatusKind.BAD: ICON_BAD,
        StatusKind.IDLE: ICON_IDLE,
        StatusKind.RUNNING: ICON_RUNNING,
        StatusKind.SELECTED: ICON_SELECTED,
    }[kind]


def colour_for(kind: str, theme_name: str = THEME_BLUE) -> str:
    """Map a loose kind string to a piece of text colour."""

    if theme_name == THEME_MONO:
        return MONO_FG
    return {
        "ok": BLUE_SUCCESS,
        "warn": BLUE_WARNING,
        "bad": BLUE_DANGER,
        "idle": BLUE_MUTED,
        "running": BLUE_ACCENT,
        "selected": BLUE_ACCENT,
        "danger": BLUE_DANGER,
        "accent": BLUE_ACCENT,
        "focus": BLUE_FOCUS,
        "muted": BLUE_MUTED,
    }.get(kind, BLUE_FG)


__all__ = [
    "BLUE_ACCENT",
    "BLUE_BG",
    "BLUE_BORDER",
    "BLUE_DANGER",
    "BLUE_FG",
    "BLUE_FOCUS",
    "BLUE_MUTED",
    "BLUE_NAV_ACTIVE",
    "BLUE_NAV_ACTIVE_FG",
    "BLUE_PANEL",
    "BLUE_SUCCESS",
    "BLUE_SURFACE",
    "BLUE_WARNING",
    "BLUE_ZEBRA",
    "MONO_BG",
    "MONO_FG",
    "THEME_BLUE",
    "THEME_MONO",
    "colour_for",
    "focus_colour",
    "mono_theme",
    "raidforge_theme",
    "select_theme_name",
    "status_icon",
    "status_style",
]
