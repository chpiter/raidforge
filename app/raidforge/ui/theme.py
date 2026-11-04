"""Theme registration and the stylesheet loader.

Two themes are registered:

``raidforge``
    The 7.2 deep-blue palette (#002B49 / #4FC3F7 / #FF5252).
``raidforge-mono``
    Pure black and white for the 16-colour VGA/fbcon console, where a dark
    brand colour is quantised to ``SGR 90`` and can become invisible.

The TCSS file references only theme variables, so the same layout works under
both palettes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .palette import (
    BLUE_ACCENT,
    BLUE_BG,
    BLUE_BORDER,
    BLUE_DANGER,
    BLUE_FG,
    BLUE_MUTED,
    BLUE_NAV_ACTIVE,
    BLUE_PANEL,
    BLUE_SUCCESS,
    BLUE_SURFACE,
    BLUE_WARNING,
    BLUE_ZEBRA,
    MONO_BG,
    MONO_FG,
    THEME_BLUE,
    THEME_MONO,
    mono_theme,
    raidforge_theme,
    select_theme_name,
)

#: Backwards-compatible names used by tests and the old screens.
MONO_THEME_NAME = THEME_MONO
DEFAULT_THEME_NAME = THEME_BLUE

#: SGR parameters that may appear in the terminal byte stream on the console.
MONO_SGR_ALLOWED = frozenset(
    {
        "", 0, 1, 22, 7, 27, 39, 49,
        30, 37, 40, 47,
        97, 107,
    }
)

#: SGR parameters that must never appear on the 16-colour console.
MONO_SGR_BANNED = frozenset(
    {
        2, 3, 4, 5, 9, 38, 48, 90, 100,
        31, 32, 33, 34, 35, 36, 91, 92, 93, 94, 95, 96, 101,
    }
)

STYLES_PATH = Path(__file__).resolve().with_name("styles") / "app.tcss"


def build_css(cfg: Any = None) -> str:
    """Return the TCSS text.

    ``cfg`` is accepted for backwards compatibility; the stylesheet no longer
    varies per install.  If the file is missing (for example a broken package
    build) return an empty string rather than crashing the console.
    """

    try:
        return STYLES_PATH.read_text(encoding="utf-8")
    except OSError:  # pragma: no cover - packaging guard
        return ""


#: Old callers imported this; keep it as the current stylesheet text.
DEFAULT_CSS = build_css()


__all__ = [
    "BLUE_ACCENT",
    "BLUE_BG",
    "BLUE_BORDER",
    "BLUE_DANGER",
    "BLUE_FG",
    "BLUE_MUTED",
    "BLUE_NAV_ACTIVE",
    "BLUE_PANEL",
    "BLUE_SUCCESS",
    "BLUE_SURFACE",
    "BLUE_WARNING",
    "BLUE_ZEBRA",
    "DEFAULT_CSS",
    "DEFAULT_THEME_NAME",
    "MONO_BG",
    "MONO_FG",
    "MONO_SGR_ALLOWED",
    "MONO_SGR_BANNED",
    "MONO_THEME_NAME",
    "build_css",
    "mono_theme",
    "raidforge_theme",
    "select_theme_name",
]
