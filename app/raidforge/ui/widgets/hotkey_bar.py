"""One-line dynamic hotkey bar.

Pages publish ``HotkeyHint`` lists; the bar rebuilds a single Rich ``Text`` so
it can never wrap and steal a second row from the table area.
"""

from __future__ import annotations

from typing import Iterable

from rich.style import Style
from rich.text import Text
from textual.widgets import Static

from ..models import HotkeyHint
from ..palette import BLUE_DANGER, MONO_FG, THEME_MONO, colour_for


class HotkeyBar(Static):
    """Bottom row of the shell; never enters the Tab focus loop."""

    can_focus = False

    def update_hints(self, hints: Iterable[HotkeyHint], theme_name: str = "raidforge") -> None:
        text = Text()
        for index, hint in enumerate(hints):
            if index:
                text.append(" │ ", style=Style(color=colour_for("muted", theme_name)))
            key_colour = (
                BLUE_DANGER
                if hint.danger and theme_name != THEME_MONO
                else (MONO_FG if theme_name == THEME_MONO else colour_for("accent", theme_name))
            )
            if not hint.enabled:
                key_colour = colour_for("muted", theme_name)
            dim = (not hint.enabled) and theme_name != THEME_MONO
            text.append(
                hint.key,
                style=Style(color=key_colour, bold=hint.enabled, dim=dim),
            )
            text.append(" ", style=Style(color=key_colour))
            text.append(
                hint.label,
                style=Style(
                    color=key_colour if hint.danger else colour_for("muted", theme_name),
                    bold=hint.danger and hint.enabled,
                    dim=dim,
                ),
            )
        self.content = text


__all__ = ["HotkeyBar"]
