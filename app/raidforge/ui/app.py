"""Textual application shell for the seven-module RAIDFORGE UI."""

from __future__ import annotations

import logging
import signal
import threading
from pathlib import Path
from typing import Callable, Optional

from textual.app import App
from textual.binding import Binding

from ..core.models import SnapshotRecord
from ..core.screenshots import take_snapshot
from .messages import Toast
from .palette import (
    THEME_BLUE,
    THEME_MONO,
    mono_theme,
    raidforge_theme,
    select_theme_name,
)
from .screens.main import MainScreen
from .state import AppState

log = logging.getLogger(__name__)


class RaidforgeApp(App[None]):
    TITLE = "RAIDFORGE"
    SUB_TITLE = "RAID management and auditable disk erase"

    CSS_PATH = "styles/app.tcss"
    ENABLE_COMMAND_PALETTE = False
    AUTO_FOCUS = None

    BINDINGS = [
        Binding("ctrl+p", "palette", "Commands", show=False, priority=True),
        Binding("f1", "help", "Help", show=False),
        Binding("question_mark", "help", "Help", show=False),
        Binding("f5", "refresh", "Refresh", show=False),
        Binding("ctrl+q", "quit_app", "Quit", priority=True),
        Binding("ctrl+c", "quit_app", "Quit", priority=True),
    ]

    def __init__(self, state: AppState) -> None:
        super().__init__()
        self.state = state
        self.snapshot_counter = 0
        self._ui_thread_id = threading.get_ident()
        self.register_theme(raidforge_theme())
        self.register_theme(mono_theme())
        self.theme = select_theme_name(state.cfg.ui.theme)

    # -- lifecycle ----------------------------------------------------------
    def on_mount(self) -> None:
        self.state.evidence.emit(
            "tui_started",
            app_version=self.state.cfg.app_version,
            dry_run=self.state.cfg.dry_run,
            theme=self.theme,
            pid=None,
        )
        self.push_screen(MainScreen(self.state))
        self.set_timer(0.2, self._focus_nav)
        self._install_signal_handlers()

    def _install_signal_handlers(self) -> None:
        if threading.current_thread() is not threading.main_thread():
            return
        for signum in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(signum, self._on_signal)
            except (ValueError, OSError):  # pragma: no cover - platform dependent
                pass

    def _on_signal(self, signum, frame) -> None:
        threading.Thread(target=self._graceful_shutdown, name="agent-drain", daemon=True).start()

    def _graceful_shutdown(self) -> None:
        runtime = getattr(self.state, "agent_runtime", None)
        if runtime is not None:
            try:
                runtime.stop(grace=10.0)
            except Exception:  # pragma: no cover
                pass
        self.state.shutdown_event.set()
        try:
            self.call_ui(self.exit)
        except Exception:  # pragma: no cover - app already gone
            pass

    def on_unmount(self) -> None:  # pragma: no cover - trivial
        self.state.evidence.emit("tui_stopped")

    def _focus_nav(self) -> None:
        main = self._main_screen()
        if main is None:
            return
        try:
            main.query_one("#nav-bar").focus()
        except Exception:  # pragma: no cover
            pass

    # -- snapshots ----------------------------------------------------------
    async def take_snapshot(self, name: str, epoch: Optional[int] = None) -> Optional[str]:
        """Capture the current screen into the evidence ``snapshots/`` folder."""

        import re

        stem = re.sub(r"^\d+-", "", name)
        self.snapshot_counter += 1
        stamped = f"{self.snapshot_counter:02d}-{stem}"
        target = await take_snapshot(self, self.state.evidence, stamped)
        if target:
            try:
                relative = Path(target).resolve().relative_to(self.state.evidence.root.resolve()).as_posix()
            except ValueError:  # pragma: no cover - custom evidence writer
                relative = str(target)
            self.state.snapshot_log.append(
                SnapshotRecord(
                    name=stamped,
                    stem=stem,
                    path=relative,
                    run_epoch=self.state.run_epoch if epoch is None else epoch,
                )
            )
        return str(target) if target else None

    def request_snapshot(self, name: str, epoch: int = 0) -> None:
        async def _capture() -> None:
            try:
                await self.take_snapshot(name, epoch=epoch)
            except Exception:  # pragma: no cover - snapshots are best effort
                pass

        self.run_worker(_capture, name=f"snapshot-{name}")

    # -- helpers ------------------------------------------------------------
    def call_ui(self, fn: Callable, *args, **kwargs) -> None:
        try:
            if threading.get_ident() == self._ui_thread_id:
                fn(*args, **kwargs)
            else:
                self.call_from_thread(fn, *args, **kwargs)
        except Exception:  # pragma: no cover - app already gone
            pass

    def push_top_screen(self, screen) -> None:
        while len(self.screen_stack) > 1:
            self.pop_screen()
        self.push_screen(screen)

    # -- global navigation --------------------------------------------------
    def _main_screen(self) -> Optional[MainScreen]:
        for screen in self.screen_stack:
            if isinstance(screen, MainScreen):
                return screen
        return None

    def action_goto(self, page_name: str) -> None:
        from .models import PageId

        main = self._main_screen()
        if main is None:
            return
        while self.screen is not main and len(self.screen_stack) > 1:
            self.pop_screen()
        try:
            main.show_page(PageId(page_name))
        except ValueError:
            return

    def action_palette(self) -> None:
        main = self._main_screen()
        if main is not None:
            main.action_palette()

    def action_help(self) -> None:
        main = self._main_screen()
        if main is not None:
            main.action_help()

    def action_refresh(self) -> None:
        main = self._main_screen()
        if main is not None:
            main.action_refresh()

    # -- quit / cancel ------------------------------------------------------
    def action_quit_app(self) -> None:
        """Ctrl+Q: cancel a running task first, exit on the second press."""

        runner = self.state.task_runner
        task_id = self.state.active_task_id
        if task_id is not None and runner is not None:
            if not self.state.cancel_event.is_set():
                if runner.cancel_active():  # type: ignore[union-attr]
                    self.state.evidence.emit("quit_requested", cancel_run=True)
                    task = next(
                        (item for item in self.state.task_queue if item.id == task_id),
                        None,
                    )
                    title = task.title if task is not None else "task"
                    try:
                        main = self._main_screen()
                        if main is not None:
                            main.post_message(
                                Toast(
                                    f"cancelling {title} - press Ctrl+Q again to exit",
                                    "warn",
                                    timeout=8.0,
                                )
                            )
                    except Exception:  # pragma: no cover
                        pass
                    return
            else:
                # Second press: exit even if the vendor command is still running.
                pass
        self.state.evidence.emit("quit_requested", cancel_run=self.state.cancel_event.is_set())
        runtime = getattr(self.state, "agent_runtime", None)
        if runtime is not None:
            try:
                runtime.stop(grace=10.0)
            except Exception:  # pragma: no cover
                pass
        self.state.shutdown_event.set()
        self.exit()


def create_app(state: AppState) -> RaidforgeApp:
    return RaidforgeApp(state)


__all__ = ["RaidforgeApp", "create_app"]
