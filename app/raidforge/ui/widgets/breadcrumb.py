"""One-line breadcrumb with optional task and focus indicators."""

from __future__ import annotations

from typing import Iterable

from rich.style import Style
from rich.text import Text
from textual.widgets import Static

from ..models import BreadcrumbItem
from ..palette import THEME_BLUE, colour_for, focus_colour


class Breadcrumb(Static):
    """Render ``Dashboard / Controllers / c0`` plus task/focus hints."""

    def __init__(self, text: str = "", **kwargs) -> None:
        super().__init__(text, **kwargs)
        self._items: list[BreadcrumbItem] = []
        self._focus_label = ""
        self._task_label = ""

    def update_items(self, items: Iterable[BreadcrumbItem]) -> None:
        self._items = list(items)
        self._refresh()

    def set_focus_label(self, label: str) -> None:
        self._focus_label = (label or "").strip()
        self._refresh()

    def set_task_label(self, label: str) -> None:
        self._task_label = (label or "").strip()
        self._refresh()

    @property
    def focus_label(self) -> str:
        return self._focus_label

    @property
    def task_label(self) -> str:
        return self._task_label

    def _theme_name(self) -> str:
        try:
            return str(self.app.theme)
        except Exception:  # pragma: no cover - widget not mounted yet
            return THEME_BLUE

    def _refresh(self) -> None:
        theme = self._theme_name()
        muted = Style(color=colour_for("muted", theme))
        text = Text()
        for index, item in enumerate(self._items):
            if index:
                text.append(" / ", style=muted)
            last = index == len(self._items) - 1
            text.append(
                item.label,
                style=Style(
                    color=colour_for("accent", theme) if last else colour_for("muted", theme),
                    bold=last,
                ),
            )
        if self._task_label:
            text.append("  ·  ", style=muted)
            text.append(
                self._task_label,
                style=Style(color=colour_for("accent", theme), bold=True),
            )
        if self._focus_label:
            text.append("  ·  ", style=muted)
            text.append("▸ Focus: ", style=Style(color=focus_colour(theme), bold=True))
            text.append(
                self._focus_label,
                style=Style(color=focus_colour(theme), bold=True),
            )
        self.content = text


__all__ = ["Breadcrumb"]
