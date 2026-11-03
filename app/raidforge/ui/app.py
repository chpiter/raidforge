"""Textual application shell (constraint 3).

The app owns nothing but the screen stack; every piece of state lives in
:class:`~raidforge.ui.state.AppContext`.

The palette is a registered monochrome theme (see :mod:`raidforge.ui.theme`),
not a per-install configuration - the console has 16 colours and any third
colour risks ending up invisible.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

from textual.app import App
from textual.binding import Binding
from textual.screen import Screen

from ..core.models import SnapshotRecord
from ..core.screenshots import take_snapshot
from .screens import DashboardScreen
from .state import AppContext
from .theme import DEFAULT_CSS, MONO_THEME_NAME, mono_theme

log = logging.getLogger(__name__)


class RaidforgeApp(App[None]):
    """Root application: fixed status/key bars plus a stack of screens."""

    TITLE = "RAIDFORGE"
    SUB_TITLE = "RAID management and auditable disk erase"

    #: The palette is fixed; a colour picker in a command palette would only
    #: invite someone to break the one rule the console cares about.
    ENABLE_COMMAND_PALETTE = False

    CSS = DEFAULT_CSS

    BINDINGS = [
        Binding("ctrl+c", "quit_app", "Quit", priority=True),
        Binding("q", "quit_app", "Quit"),
    ]

    def __init__(self, ctx: AppContext) -> None:
        super().__init__()
        self.ctx = ctx
        self.snapshot_counter = 0
        # Order matters: register first, then select, or Textual silently falls
        # back to the default (colour) theme.
        self.register_theme(mono_theme())
        self.theme = MONO_THEME_NAME

    # -- lifecycle ----------------------------------------------------------
    def on_mount(self) -> None:
        """Show the dashboard and record the session start."""

        self.ctx.evidence.emit(
            "tui_started",
            app_version=self.ctx.cfg.app_version,
            dry_run=self.ctx.cfg.dry_run,
            pid=None,
        )
        self.push_screen(DashboardScreen(self.ctx))

    def on_unmount(self) -> None:  # pragma: no cover - trivial
        self.ctx.evidence.emit("tui_stopped")

    # -- helpers ------------------------------------------------------------
    async def take_snapshot(self, name: str, epoch: Optional[int] = None) -> Optional[str]:
        """Capture the current screen into the evidence ``snapshots/`` folder.

        Args:
            name: Caller supplied stem, e.g. ``03-disk-list``.
            epoch: Erase run this screen belongs to. Defaults to the context's
                current epoch; the confirm/progress screens pass theirs
                explicitly so a late capture cannot be attributed to the next
                run.

        Returns:
            Absolute path of the written SVG, or ``None``.
        """

        from pathlib import Path
        import re

        # Call sites pass names such as "03-disk-list"; the counter is the
        # authority for ordering, so drop the caller's own number instead of
        # producing "01-03-disk-list".
        stem = re.sub(r"^\d+-", "", name)
        self.snapshot_counter += 1
        stamped = f"{self.snapshot_counter:02d}-{stem}"
        target = await take_snapshot(self, self.ctx.evidence, stamped)
        if target:
            try:
                relative = Path(target).resolve().relative_to(self.ctx.evidence.root.resolve()).as_posix()
            except ValueError:  # pragma: no cover - custom evidence writer
                relative = str(target)
            self.ctx.snapshot_log.append(
                SnapshotRecord(
                    name=stamped,
                    stem=stem,
                    path=relative,
                    run_epoch=self.ctx.run_epoch if epoch is None else epoch,
                )
            )
        return str(target) if target else None

    def push_top_screen(self, screen: Screen) -> None:
        """Replace the whole stack with a single screen (used on restart)."""

        while len(self.screen_stack) > 1:
            self.pop_screen()
        self.push_screen(screen)

    def call_ui(self, fn: Callable, *args, **kwargs) -> None:
        """Hop back to the UI thread, tolerating a shutting-down app.

        Worker threads outlive the app by design (they are daemons), so a late
        callback must not raise ``NoActiveAppError`` into the thread.
        """

        try:
            self.call_from_thread(fn, *args, **kwargs)
        except Exception:  # pragma: no cover - app already gone
            pass

    # -- per-run publish ----------------------------------------------------
    def start_run_publish(self, epoch: int) -> None:
        """Capture the progress screen, then publish the cumulative report.

        The capture is awaited *before* the daemon publisher starts so the DOCX
        always contains this run's progress screenshot.
        """

        async def _capture_then_publish() -> None:
            try:
                from .screens.progress import ProgressScreen
                from .screens.raid_progress import RaidProgressScreen

                if self.ctx.cfg.evidence.snapshot_on_screen_change:
                    if isinstance(self.screen, ProgressScreen):
                        await self.take_snapshot("05-progress", epoch=epoch)
                    elif isinstance(self.screen, RaidProgressScreen):
                        await self.take_snapshot("05-raid-progress", epoch=epoch)
            except Exception:  # pragma: no cover - screenshots are best effort
                pass
            self.ctx.spawn(self._publish_run, name=f"publish-run-{epoch}")

        self.run_worker(_capture_then_publish, name=f"capture-publish-{epoch}")

    def _publish_run(self) -> None:
        """Daemon worker: report + manifest + best-effort upload for one run."""

        from ..core.publish import PublishOptions, publish_from_context

        def status(text: str) -> None:
            self.ctx.publish_state.set_status(text)
            if not self.ctx.shutdown_event.is_set():
                self.call_ui(self.show_publish_status, text)

        publish_from_context(
            self.ctx,
            PublishOptions(
                phase="run",
                upload=self.ctx.cfg.report.enabled,
                queue_on_failure=False,
                budget_seconds=self.ctx.cfg.report.run_upload_seconds,
                skip_route_wait=self.ctx.network_ready,
                on_status=status,
            ),
        )

    def show_publish_status(self, text: str) -> None:
        """Mirror the publisher status without touching a detached screen."""

        try:
            screen = self.screen
            setter = getattr(screen, "set_message", None)
            if callable(setter):
                setter(text)
            refresh = getattr(screen, "refresh_publish_status", None)
            if callable(refresh):
                refresh()
        except Exception:  # pragma: no cover - app shutting down
            pass

    # -- actions ------------------------------------------------------------
    def action_quit_app(self) -> None:
        """The single exit decision point.

        While an erase run is in flight the first Q only *cancels* it: killing
        the process outright would orphan the running vendor command and its
        result would never reach the evidence log.  The operator presses Q again
        (or Esc) once the run has stopped.
        """

        from .screens.progress import ProgressScreen  # local import: avoid a cycle
        from .screens.raid_progress import RaidProgressScreen

        # The run state, not cancel_event, decides "in flight": _finish clears
        # cancel_event, so an already-finished run would otherwise be treated
        # as running and the first Q would park it in a bogus cancelling state.
        active = getattr(self.screen, "is_run_active", None)
        running = (
            isinstance(self.screen, (ProgressScreen, RaidProgressScreen))
            and callable(active)
            and active()
        )
        if running:
            self.ctx.cancel_event.set()
            self.ctx.evidence.emit("quit_requested", cancel_run=True)
            try:
                self.screen.request_cancel()
            except Exception:  # pragma: no cover - defensive
                pass
            return

        self.ctx.evidence.emit("quit_requested", cancel_run=self.ctx.cancel_event.is_set())
        self.ctx.shutdown_event.set()
        self.exit()


def create_app(ctx: AppContext) -> RaidforgeApp:
    """Build the application.

    Args:
        ctx: Shared application context.

    Returns:
        A ready-to-run application instance.
    """

    return RaidforgeApp(ctx)


__all__ = ["RaidforgeApp", "create_app"]
