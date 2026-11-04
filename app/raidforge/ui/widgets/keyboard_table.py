"""Shared keyboard bindings for every DataTable in the app."""

from __future__ import annotations

from textual.binding import Binding
from textual.widgets import DataTable


class KeyboardTable(DataTable):
    """DataTable with Vim-style movement; everything else is built in.

    Textual 7.5.0 already binds Up/Down, Home/End, PageUp/PageDown and Enter.
    J/K are added so the operator can keep their hands on the home row.
    """

    BINDINGS = [
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
    ]

    def __init__(self, **kwargs) -> None:
        kwargs.setdefault("cursor_type", "row")
        super().__init__(**kwargs)


__all__ = ["KeyboardTable"]
