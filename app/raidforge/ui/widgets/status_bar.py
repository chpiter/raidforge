"""One-line status bar: server SN, IP address and login user."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import Static
from rich.text import Text

from ..state import AppState


class StatusBar(Widget):
    """Top row of the shell.

    It updates only when the state changes; there is deliberately no
    ``set_interval`` because the application never polls the host.
    """

    def compose(self) -> ComposeResult:
        yield Static("", id="status-sn", classes="status-left")
        yield Static("", id="status-ip", classes="status-mid")
        yield Static("", id="status-user", classes="status-right")

    def update_state(self, state: AppState) -> None:
        sn = state.server_sn or "-"
        ip = state.ip_address or _ip_from_detail(state.network_detail) or "-"
        user = state.login_user or "-"

        self.query_one("#status-sn", Static).update(
            Text.assemble(("SN ", "bold"), (sn, ""))
        )
        self.query_one("#status-ip", Static).update(
            Text.assemble(("IP ", "bold"), (ip, ""))
        )
        self.query_one("#status-user", Static).update(
            Text.assemble(("User ", "bold"), (user, ""))
        )


class SessionStatusBar(StatusBar):
    """Legacy shim for the old screen stack; the new shell uses StatusBar."""

    def __init__(self, ctx, screen_name: str = "") -> None:
        super().__init__()
        self.ctx = ctx
        self.screen_name = screen_name

    def on_mount(self) -> None:
        self.update_state(self.ctx)

    def refresh_content(self) -> None:
        self.update_state(self.ctx)

    def set_message(self, text: str) -> None:
        self.ctx.last_message = text


def _ip_from_detail(detail: str) -> str:
    """``"eno1 192.168.40.50"`` -> ``"192.168.40.50"``."""

    parts = (detail or "").split()
    for part in reversed(parts):
        if part.count(".") == 3 and all(piece.isdigit() for piece in part.split(".")):
            return part
    return ""


__all__ = ["StatusBar"]
