"""Root shell: status bar, top tabs, breadcrumb, pages and hotkey bar."""

from __future__ import annotations

from typing import List, Optional

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import ContentSwitcher

from ..messages import (
    BreadcrumbChanged,
    DataRefreshed,
    HotkeysChanged,
    NavigateRequested,
    ReportStatusChanged,
    TaskStateChanged,
    Toast,
)
from ..models import HotkeyHint, PageId, RefreshScope, TaskKind
from ..pages.base import StatefulPage
from ..pages.controllers import ControllersPage
from ..pages.dashboard import DashboardPage
from ..pages.evidence import EvidencePage
from ..pages.physical import PhysicalPage
from ..pages.settings import SettingsPage
from ..pages.tasks import TasksPage
from ..pages.virtual import VirtualPage
from ..services import Services
from ..state import AppState
from ..tasks import TaskRunner, make_task, refresh_job
from ..widgets.breadcrumb import Breadcrumb
from ..widgets.hotkey_bar import HotkeyBar
from ..widgets.nav_bar import NavBar
from ..widgets.status_bar import StatusBar

PAGE_CLASSES = {
    PageId.DASHBOARD: DashboardPage,
    PageId.CONTROLLERS: ControllersPage,
    PageId.PHYSICAL: PhysicalPage,
    PageId.VIRTUAL: VirtualPage,
    PageId.TASKS: TasksPage,
    PageId.EVIDENCE: EvidencePage,
    PageId.SETTINGS: SettingsPage,
}

SNAPSHOT_NAMES = {
    PageId.DASHBOARD: "10-dashboard",
    PageId.CONTROLLERS: "11-controllers",
    PageId.PHYSICAL: "12-physical",
    PageId.VIRTUAL: "13-virtual",
    PageId.TASKS: "14-tasks",
    PageId.EVIDENCE: "15-evidence",
    PageId.SETTINGS: "16-settings",
}


class MainScreen(Screen):
    """The only persistent screen; detail/wizard screens are pushed on top."""

    BINDINGS = [
        Binding("ctrl+1", "goto('dashboard')", "Dashboard", show=False, priority=True),
        Binding("ctrl+2", "goto('controllers')", "Controllers", show=False, priority=True),
        Binding("ctrl+3", "goto('physical')", "Physical", show=False, priority=True),
        Binding("ctrl+4", "goto('virtual')", "Virtual", show=False, priority=True),
        Binding("ctrl+5", "goto('tasks')", "Tasks", show=False, priority=True),
        Binding("ctrl+6", "goto('evidence')", "Evidence", show=False, priority=True),
        Binding("ctrl+7", "goto('settings')", "Settings", show=False, priority=True),
        Binding("f1", "help", "Help", show=False),
        Binding("f5", "refresh", "Refresh", show=False),
        Binding("ctrl+p", "palette", "Commands", show=False, priority=True),
        Binding("f7", "create", "Create volume", show=False),
        Binding("f8", "erase", "Erase", show=False),
        Binding("f9", "delete_volume", "Delete VD", show=False),
        Binding("f10", "clear_config", "Clear config", show=False),
        Binding("escape", "back", "Back", show=False),
    ]

    def __init__(self, state: AppState) -> None:
        super().__init__()
        self.state = state
        self._initial_snapshot = False

    # -- layout -------------------------------------------------------------
    def compose(self) -> ComposeResult:
        with Vertical(id="app-root"):
            yield StatusBar(id="status-bar")
            yield NavBar(id="nav-bar")
            yield Breadcrumb("", id="breadcrumb")
            with ContentSwitcher(initial="page-dashboard", id="pages"):
                for page in PageId:
                    yield PAGE_CLASSES[page](self.state)
            yield HotkeyBar("", id="hotkey-bar")

    def on_mount(self) -> None:
        self.state.set_page(PageId.DASHBOARD)
        self.query_one(NavBar).set_active(PageId.DASHBOARD)
        self.query_one(NavBar).set_cursor(0)
        self._update_chrome()
        self._update_compact()
        self.set_timer(0.1, self._focus_nav)
        self.state.evidence.emit("ui_started", page=PageId.DASHBOARD.value)

        self.state.services = Services(self.state)
        self.state.task_runner = TaskRunner(
            self.state,
            self.app,
            self.broadcast,
            self.state.services,  # type: ignore[arg-type]
        )
        task = make_task(TaskKind.REFRESH, "Initial inventory")
        self.state.task_runner.submit(  # type: ignore[union-attr]
            task,
            refresh_job(self.state.services, RefreshScope.ALL),  # type: ignore[arg-type]
        )
        self.state.spawn(self._probe_network, name="network-probe")

    def _focus_nav(self) -> None:
        try:
            self.query_one(NavBar).focus()
        except Exception:  # pragma: no cover
            pass

    def _probe_network(self) -> None:
        """One-shot network label; there is no periodic polling."""

        try:
            from ...core.report import network_summary

            summary = network_summary()
        except Exception:  # pragma: no cover - no iproute on a bench
            summary = ""
        self.app.call_ui(self._set_network, summary)

    def _set_network(self, summary: str) -> None:
        self.state.network_detail = summary or ""
        self.state.ip_address = _ip_from_summary(summary)
        self.broadcast(DataRefreshed(RefreshScope.REPORT, generation=self.state.refresh.generation))
        self._update_chrome()

    def on_unmount(self) -> None:
        runner = self.state.task_runner
        if runner is not None:
            try:
                runner.shutdown()  # type: ignore[union-attr]
            except Exception:  # pragma: no cover
                pass

    # -- navigation ---------------------------------------------------------
    def page_for(self, page: PageId) -> StatefulPage:
        return self.query_one(f"#page-{page.value}", StatefulPage)

    def current_page(self) -> StatefulPage:
        return self.page_for(self.state.active_page)

    def show_page(self, page: PageId, push_history: bool = True) -> None:
        if page is self.state.active_page:
            return
        try:
            self.current_page().save_scroll()
        except Exception:  # pragma: no cover - initial mount
            pass
        if push_history:
            self.state.page_history.append(self.state.active_page)
            self.state.page_history = self.state.page_history[-20:]
        self.state.set_page(page)
        self.query_one(ContentSwitcher).current = f"page-{page.value}"
        self.query_one(NavBar).set_active(page)
        self.state.evidence.emit("ui_page_changed", page=page.value)
        self._update_chrome()
        try:
            target = self.page_for(page)
            target.apply_layout_mode(self.state.layout_mode)
            target.restore_scroll()
            self.call_after_refresh(target.focus_primary)
            if page is PageId.EVIDENCE and not self.state.evidence_sessions:
                target._scan()  # type: ignore[attr-defined]
        except Exception:  # pragma: no cover
            pass
        self.request_snapshot(SNAPSHOT_NAMES[page])

    def action_goto(self, page_name: str) -> None:
        try:
            self.show_page(PageId(page_name))
        except ValueError:
            return

    def action_back(self) -> None:
        if self.state.page_history:
            target = self.state.page_history.pop()
            self.show_page(target, push_history=False)
            return
        self.toast("already at the top level")

    # -- broadcast ----------------------------------------------------------
    def broadcast(self, message) -> None:
        for page in self.query(StatefulPage):
            try:
                clone = message.clone() if hasattr(message, "clone") else message
                page.post_message(clone)
            except Exception:  # pragma: no cover - page may be mid-removal
                continue
        if isinstance(message, DataRefreshed) and message.scope is RefreshScope.ALL:
            if not self._initial_snapshot:
                self._initial_snapshot = True
                self.request_snapshot("10-dashboard")
        self._update_chrome()

    def on_navigate_requested(self, message: NavigateRequested) -> None:
        self.show_page(message.page)

    def on_hotkeys_changed(self, message: HotkeysChanged) -> None:
        if message.page is not None and message.page is not self.state.active_page:
            return
        self.query_one(HotkeyBar).update_hints(message.hints, self.app.theme)

    def on_breadcrumb_changed(self, message: BreadcrumbChanged) -> None:
        if message.page is not None and message.page is not self.state.active_page:
            return
        self.query_one(Breadcrumb).update_items(message.items)

    def on_toast(self, message: Toast) -> None:
        self.state.last_message = message.text
        severity = {"warn": "warning", "error": "error"}.get(message.level, "information")
        try:
            self.app.notify(message.text, severity=severity, timeout=3)
        except Exception:  # pragma: no cover - older Textual
            pass

    def on_data_refreshed(self, message: DataRefreshed) -> None:
        self._update_chrome()
        if not self._initial_snapshot and message.scope is RefreshScope.ALL:
            self._initial_snapshot = True
            self.request_snapshot("10-dashboard")

    def on_task_state_changed(self, message: TaskStateChanged) -> None:
        self._update_chrome()

    def on_report_status_changed(self, message: ReportStatusChanged) -> None:
        self._update_chrome()

    # -- chrome -------------------------------------------------------------
    def _update_chrome(self) -> None:
        try:
            self.query_one(StatusBar).update_state(self.state)
            self.query_one(Breadcrumb).update_items(self.state.breadcrumb_items())
            self.query_one(NavBar).set_active(self.state.active_page)
            self.query_one(HotkeyBar).update_hints(self.current_page().hotkeys(), self.app.theme)
        except Exception:  # pragma: no cover - initial compose
            pass

    def on_resize(self) -> None:
        self._update_compact()

    def _update_compact(self) -> None:
        try:
            compact = self.size.width <= 90 or self.size.height <= 30
            self.query_one("#app-root").set_class(compact, "-compact")
            from ..models import LayoutMode

            mode = (
                LayoutMode.WIDE
                if self.size.width >= 110 and self.size.height > 30
                else LayoutMode.STANDARD
            )
            if mode is not self.state.layout_mode:
                self.state.layout_mode = mode
                for page in self.query(StatefulPage):
                    page.apply_layout_mode(mode)
        except Exception:  # pragma: no cover
            pass

    # -- global actions -----------------------------------------------------
    def action_refresh(self) -> None:
        if self.state.active_task_id is not None:
            self.toast("a task is running; refresh after it finishes")
            return
        runner = self.state.task_runner
        services = self.state.services
        if runner is None or services is None:
            return
        task = make_task(TaskKind.REFRESH, "Manual refresh")
        runner.submit(task, refresh_job(services, RefreshScope.ALL))  # type: ignore[union-attr]
        self.toast(f"refresh queued as {task.id}")

    def _delegate(self, action: str) -> None:
        page = self.current_page()
        handler = getattr(page, f"action_{action}", None)
        if callable(handler):
            handler()
        else:
            self.toast(f"{action} is not available on {page.PAGE.title}")

    def action_create(self) -> None:
        self._delegate("create")

    def action_erase(self) -> None:
        self._delegate("erase")

    def action_delete_volume(self) -> None:
        self._delegate("delete")

    def action_clear_config(self) -> None:
        self._delegate("clear")

    def action_help(self) -> None:
        from .help import HelpScreen

        self.app.push_screen(HelpScreen(self.state, self.current_page()))

    def action_palette(self) -> None:
        from .command_palette import Command, CommandPaletteModal

        commands = self.commands()
        self.app.push_screen(CommandPaletteModal(commands))

    def commands(self) -> List["Command"]:
        from .command_palette import Command

        page = self.current_page()
        return [
            Command("Dashboard", "goto('dashboard')", "Ctrl+1", namespace=self),
            Command("Controllers", "goto('controllers')", "Ctrl+2", namespace=self),
            Command("Physical", "goto('physical')", "Ctrl+3", namespace=self),
            Command("Virtual", "goto('virtual')", "Ctrl+4", namespace=self),
            Command("Tasks", "goto('tasks')", "Ctrl+5", namespace=self),
            Command("Evidence", "goto('evidence')", "Ctrl+6", namespace=self),
            Command("Settings", "goto('settings')", "Ctrl+7", namespace=self),
            Command("Refresh all", "refresh", "F5", enabled=self.state.active_task_id is None, namespace=self),
            Command("Create volume", "create", "F7", enabled=hasattr(page, "action_create"), namespace=self),
            Command("Erase selected disks", "erase", "F8", enabled=hasattr(page, "action_erase"), namespace=self),
            Command("Delete virtual drive", "delete_volume", "F9", enabled=hasattr(page, "action_delete"), namespace=self),
            Command("Clear controller config", "clear_config", "F10", enabled=hasattr(page, "action_clear"), namespace=self),
            Command("Export SVG snapshot", "export_snapshot", "", namespace=self),
            Command("Toggle theme", "toggle_theme", "", namespace=self),
            Command("Help", "help", "F1", namespace=self),
            Command("Quit", "quit_app", "Ctrl+Q", namespace=self.app),
        ]

    def action_export_snapshot(self) -> None:
        self.request_snapshot("manual")

    def action_toggle_theme(self) -> None:
        from ..palette import THEME_BLUE, THEME_MONO

        self.app.theme = THEME_MONO if self.app.theme == THEME_BLUE else THEME_BLUE
        self._update_chrome()
        self.toast(f"theme: {self.app.theme}")

    def toast(self, text: str, level: str = "info") -> None:
        self.state.last_message = text
        self.on_toast(Toast(text, level))

    # -- snapshots ----------------------------------------------------------
    def request_snapshot(self, name: str, epoch: int = 0) -> None:
        async def _capture() -> None:
            try:
                await self.app.take_snapshot(name, epoch=epoch)  # type: ignore[attr-defined]
            except Exception:  # pragma: no cover - snapshots are best effort
                pass

        self.run_worker(_capture, name=f"snapshot-{name}")


def _ip_from_summary(summary: str) -> str:
    for part in reversed((summary or "").split()):
        if part.count(".") == 3 and all(piece.isdigit() for piece in part.split(".")):
            return part
    return ""


__all__ = ["MainScreen"]
