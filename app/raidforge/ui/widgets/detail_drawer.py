"""Bottom detail drawer that only occupies height when a row is selected."""

from __future__ import annotations

from typing import Union

from rich.text import Text
from textual.widgets import Static


class DetailDrawer(Static):
    can_focus = True
    def __init__(self, *, id: str) -> None:
        super().__init__("", id=id, classes="detail-drawer -hidden")

    def show(self, value: Union[str, Text]) -> None:
        self.content = value
        self.set_class(False, "-hidden")

    def hide(self) -> None:
        self.content = ""
        self.set_class(True, "-hidden")


__all__ = ["DetailDrawer"]
