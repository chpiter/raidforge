"""Bottom shortcut bar (fixed, never scrolls)."""

from __future__ import annotations

from typing import Iterable, Tuple

from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import Static


class KeyBar(Widget):
    """Context sensitive hint row docked at the bottom of the screen."""

    def __init__(self, hints: Iterable[Tuple[str, str]] = ()) -> None:
        super().__init__(id="key-bar")
        self.hints = list(hints)
        self._line = Static("")

    def compose(self) -> ComposeResult:
        yield self._line

    def on_mount(self) -> None:
        """Render the hint string."""

        self.update_hints(self.hints)

    def update_hints(self, hints: Iterable[Tuple[str, str]]) -> None:
        """Replace the shown hints; ``hints`` is a list of ``(key, label)``."""

        self.hints = list(hints)
        parts = [f"[b]{key}[/b] {label}" for key, label in self.hints]
        self._line.update("   ".join(parts) if parts else "")
