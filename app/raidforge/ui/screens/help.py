"""F1 help: global keys plus the current page's context keys."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Label, Static

from ..focus import capture_focus
from ..models import PageId
from ..state import AppState


class HelpScreen(ModalScreen[None]):
    BINDINGS = [Binding("escape", "close", "Close", priority=True)]

    def __init__(self, state: AppState, page) -> None:
        super().__init__()
        self._focus = capture_focus()
        self.state = state
        self.page = page

    def compose(self) -> ComposeResult:
        with Vertical(id="help-dialog"):
            yield Label(f"[b]Help — {self.state.active_page.title}[/b]", classes="title")
            with VerticalScroll(id="help-body"):
                yield Static(self._text())
            with Horizontal(id="palette-actions"):
                yield Button("Close [Esc]", id="help-close")

    def _text(self) -> str:
        lines = [
            "Global",
            "  Ctrl+1..7   go to Dashboard / Controllers / Physical / Virtual / Tasks / Evidence / Settings",
            "  Ctrl+Q      quit (a running task is cancelled first)",
            "  Ctrl+P      command palette",
            "  F1          this help",
            "  F5          full refresh",
            "  Esc         back / close",
            "",
            f"Context — {self.state.active_page.title}",
        ]
        for hint in self.page.hotkeys():
            state = "enabled" if hint.enabled else "disabled"
            danger = "  [danger]" if hint.danger else ""
            lines.append(f"  {hint.key:<10} {hint.label} ({state}){danger}")
        lines.extend(
            [
                "",
                "Destructive actions always pass through ConfirmModal.",
                "Erase/create/delete: type yes.  Clear controller config: type the controller id (e.g. c0).",
                "The full CONFIRM-{server_sn} token is shown for audit but is not typed.",
            ]
        )
        return "\n".join(lines)

    def on_button_pressed(self, _event: Button.Pressed) -> None:
        self.dismiss(None)

    def on_unmount(self) -> None:
        self._focus.restore()

    def action_close(self) -> None:
        self.dismiss(None)


__all__ = ["HelpScreen"]
