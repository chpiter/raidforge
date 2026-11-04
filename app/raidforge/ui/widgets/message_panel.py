"""Persistent bottom-right message box for logs, task lines and toasts."""

from __future__ import annotations

from collections import deque
from typing import Deque, Iterable, Tuple

from rich.text import Text
from textual.widgets import Static

from ..palette import colour_for

_MAX_LINES = 5
_MAX_CHARS = 100


class MessagePanel(Static):
    """Non-focusable overlay; the shell feeds it recent :class:`LogLine` tuples."""

    can_focus = False

    def __init__(self, **kwargs) -> None:
        super().__init__("", id="message-panel", **kwargs)
        self._lines: Deque[Tuple[str, str]] = deque(maxlen=_MAX_LINES)

    def on_mount(self) -> None:
        self.border_title = "messages"
        self._refresh_text()

    def push(self, text: str, level: str = "info") -> None:
        line = " ".join(str(text or "").split())
        if not line:
            return
        self._lines.append((level, line[:_MAX_CHARS]))
        self._refresh_text()

    def extend(self, lines: Iterable[Tuple[str, str]]) -> None:
        for level, text in lines:
            line = " ".join(str(text or "").split())
            if line:
                self._lines.append((level, line[:_MAX_CHARS]))
        self._refresh_text()

    def _refresh_text(self) -> None:
        try:
            theme = self.app.theme
        except Exception:  # pragma: no cover - not mounted yet
            theme = "raidforge"
        text = Text()
        if not self._lines:
            text.append("no messages yet", style=colour_for("muted", theme))
        else:
            for index, (level, line) in enumerate(self._lines):
                if index:
                    text.append("\n")
                kind = {"warn": "warn", "error": "bad"}.get(level, "muted")
                text.append(line, style=colour_for(kind, theme))
        self.update(text)


__all__ = ["MessagePanel"]
