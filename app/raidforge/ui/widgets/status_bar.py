"""Top status bar (fixed, never scrolls)."""

from __future__ import annotations


from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import Static

from ..state import AppContext


class SessionStatusBar(Widget):
    """Server identity, tool in use, network state, session id."""

    DEFAULT_CLASSES = ""

    def __init__(self, ctx: AppContext, screen_name: str = "Dashboard") -> None:
        super().__init__(id="status-bar")
        self.ctx = ctx
        self.screen_name = screen_name
        self._left = Static("", classes="kv-value")
        # ``sb-right`` instead of ``:last-child`` - Textual 0.89 (the version
        # vendored into the ISO) only understands :first-of-type/:last-of-type.
        self._right = Static("", classes="kv-value sb-right")

    def compose(self) -> ComposeResult:
        yield self._left
        yield self._right

    def on_mount(self) -> None:
        """Paint once and subscribe to periodic refreshes."""

        self.refresh_content()
        interval = self.ctx.cfg.ui.refresh_interval
        if interval and interval > 0:
            self.set_interval(interval, self.refresh_content)

    def refresh_content(self) -> None:
        """Recompute both halves of the bar."""

        ctx = self.ctx
        detection = ctx.detection
        tool = detection.tool if detection else "detecting..."
        controllers = len(detection.controllers) if detection else 0
        disk_count = len(ctx.disks)
        selected = len(ctx.selection)

        net = "up" if ctx.network_ready else "down"
        serial = ctx.server_id.serial
        if ctx.server_id.is_fallback:
            serial = f"{serial} (derived)"

        self._left.update(
            f"[b]{self.screen_name}[/b]  SN:[b]{serial}[/b]  tool:{tool}  "
            f"ctrl:{controllers}  disk:{disk_count}  sel:{selected}"
        )
        self._right.update(f"net:{net}  session:{ctx.evidence.session_id}")

    def set_screen_name(self, name: str) -> None:
        """Update the leading screen label."""

        self.screen_name = name
        self.refresh_content()
