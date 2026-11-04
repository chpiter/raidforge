"""One-line breadcrumb for the current page trail."""

from __future__ import annotations

from typing import Iterable

from rich.text import Text
from textual.widgets import Static

from ..models import BreadcrumbItem


class Breadcrumb(Static):
    """Render ``Dashboard / Controllers / c0`` in exactly one row."""

    def update_items(self, items: Iterable[BreadcrumbItem]) -> None:
        trail = list(items)
        if not trail:
            self.update("")
            return
        text = Text()
        for index, item in enumerate(trail):
            if index:
                text.append(" / ")
            style = "bold" if index == len(trail) - 1 else ""
            text.append(item.label, style=style)
        self.content = text


__all__ = ["Breadcrumb"]
