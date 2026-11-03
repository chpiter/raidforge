"""Base screen providing the fixed top status bar and bottom key bar.

Subclasses override :meth:`build_content` instead of ``compose`` so the chrome
can never be dropped by accident (constraint 3).
"""

from __future__ import annotations

from typing import Iterable, List, Optional, Tuple

from textual.app import ComposeResult
from textual.containers import Container
from textual.screen import Screen
from textual.widgets import Static

from ..state import AppContext
from ..widgets import KeyBar, SessionStatusBar


class BaseScreen(Screen):
    """Common chrome: status bar above, key hints below, content in between."""

    SCREEN_NAME: str = "Base"
    HINTS: List[Tuple[str, str]] = [("Q", "quit")]

    def __init__(self, ctx: AppContext) -> None:
        super().__init__()
        self.ctx = ctx
        self._status_line = Static("", id="log")

    # -- layout -------------------------------------------------------------
    def compose(self) -> ComposeResult:
        yield SessionStatusBar(self.ctx, self.SCREEN_NAME)
        with Container(id="content"):
            yield from self.build_content()
        yield KeyBar(self.HINTS)

    def build_content(self) -> Iterable[object]:
        """Yield the widgets that make up this screen's body."""

        return []

    # -- helpers ------------------------------------------------------------
    def set_message(self, text: str) -> None:
        """Update the shared status line at the bottom of the content area."""

        self.ctx.last_message = text
        try:
            self._status_line.update(text)
        except Exception:  # pragma: no cover - widget may be detached
            pass

    def status_line(self) -> Static:
        """Return the shared status widget; mount it inside your content."""

        return self._status_line

    def schedule_snapshot(self, name: str, epoch: Optional[int] = None) -> None:
        """Capture the current screen into the evidence book.

        Wrapped in a coroutine *function* because ``run_worker`` expects a
        callable; handing it a bare coroutine object silently drops the task.

        Args:
            name: Snapshot stem, e.g. ``05-progress``.
            epoch: Erase run this screen belongs to (passed by the confirm and
                progress screens so the report can pair screenshots with runs).
        """

        if not self.ctx.cfg.evidence.snapshot_on_screen_change:
            return

        async def _capture() -> None:
            await self.app.take_snapshot(name, epoch=epoch)  # type: ignore[attr-defined]

        self.run_worker(_capture, name=f"snapshot-{name}")

