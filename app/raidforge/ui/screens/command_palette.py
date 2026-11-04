"""Ctrl+P command palette, executed through App.run_action."""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import Input, Label, Static

from ..focus import capture_focus


@dataclass
class Command:
    label: str
    action: str
    hint: str = ""
    enabled: bool = True
    namespace: Optional[object] = None


class CommandPaletteModal(Screen[None]):
    BINDINGS = [Binding("escape", "close", "Close", priority=True)]

    def __init__(self, commands: List[Command]) -> None:
        super().__init__()
        self.commands = commands
        self.filtered: List[Command] = list(commands)
        self.index = 0
        self._list = Static("", id="palette-list")

    def compose(self) -> ComposeResult:
        with Vertical(id="palette-dialog"):
            yield Label("[b]Commands[/b]", classes="title")
            yield Input(placeholder="type to filter", id="palette-input")
            yield self._list
            yield Static("Up/Down or J/K select · Enter run · Esc close", classes="muted")

    def on_mount(self) -> None:
        self._focus = capture_focus()
        self._refresh_list()
        self.query_one("#palette-input", Input).focus()

    def _refresh_list(self) -> None:
        text = Text()
        for position, command in enumerate(self.filtered):
            if position:
                text.append("\n")
            selected = position == self.index
            marker = "▶ " if selected else "  "
            hint = f"  [{command.hint}]" if command.hint else ""
            suffix = "" if command.enabled else "  (unavailable)"
            style = "bold reverse" if selected and command.enabled else ""
            text.append(f"{marker}{command.label}{hint}{suffix}", style=style)
        if not self.filtered:
            text.append("(no matching command)")
        self._list.update(text)

    def on_input_changed(self, event: Input.Changed) -> None:
        query = event.value.strip().lower()
        self.filtered = [
            command
            for command in self.commands
            if not query or query in command.label.lower() or query in command.hint.lower()
        ]
        self.index = 0
        self._refresh_list()

    async def on_input_submitted(self, _event: Input.Submitted) -> None:
        await self._run_selected()

    async def on_key(self, event) -> None:
        if event.key in ("down", "j"):
            event.stop()
            if self.filtered:
                self.index = (self.index + 1) % len(self.filtered)
                self._refresh_list()
        elif event.key in ("up", "k"):
            event.stop()
            if self.filtered:
                self.index = (self.index - 1) % len(self.filtered)
                self._refresh_list()

    async def _run_selected(self) -> None:
        if not (0 <= self.index < len(self.filtered)):
            return
        command = self.filtered[self.index]
        if not command.enabled:
            return
        self.dismiss(None)
        try:
            await self.app.run_action(command.action, default_namespace=command.namespace)
        except Exception:  # pragma: no cover - action may have been removed
            pass

    def on_unmount(self) -> None:
        self._focus.restore()

    def action_close(self) -> None:
        self.dismiss(None)


__all__ = ["Command", "CommandPaletteModal"]
