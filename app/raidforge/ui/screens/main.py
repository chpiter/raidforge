"""Root shell: status bar, top tabs, breadcrumb, pages and hotkey bar."""

from __future__ import annotations

import time
from typing import List, Optional

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import ContentSwitcher

from .. import logbus
from ..messages import (
    AgentStatusChanged,
    BreadcrumbChanged,
    DataRefreshed,
    HotkeysChanged,
    NavigateRequested,
    QueueLengthChanged,
    RemoteTaskConfirmRequested,
    ReportStatusChanged,
    TaskFinished,
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
from ..focus import FocusLabelMixin
from ..services import Services
from ..state import AppState
from ..tasks import TaskRunner, make_task, refresh_job
from ..widgets.breadcrumb import Breadcrumb
from ..widgets.hotkey_bar import HotkeyBar
from ..widgets.message_panel import MessagePanel
from ..widgets.nav_bar import NavBar
from ..widgets.status_bar import StatusBar
from ..widgets.agent_status import AgentStatus
from ..screens.remote_task_confirm import RemoteTaskConfirmScreen
from ...agent.models import ConfirmDecision

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


class MainScreen(FocusLabelMixin, Screen):
    """The only persistent screen; detail/wizard screens are pushed on top."""

    BINDINGS = [
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
        with Vertical(id="message-dock"):
            yield MessagePanel()

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
        self.set_interval(0.25, self._drain_messages)
        self._start_agent()

    def _start_agent(self) -> None:
        """Agent 在首帧之后启动；任何失败都只影响状态栏。"""

        try:
            self.query_one(StatusBar).mount(AgentStatus(id="status-agent"))
        except Exception:  # pragma: no cover - compose timing
            pass
        try:
            from ...agent.runtime import build_agent

            self.state.agent_runtime = build_agent(self.state, ui_post=self._agent_ui_post)
            runtime = self.state.agent_runtime
            if runtime is not None:
                self.set_timer(0.1, runtime.start)
        except Exception:  # pragma: no cover - Agent 故障不得影响 TUI
            self.state.agent_runtime = None

    def _agent_ui_post(self, message) -> None:
        try:
            self.app.call_ui(lambda: self.post_message(message))
        except Exception:
            pass

    def _agent_status_widget(self) -> Optional[AgentStatus]:
        try:
            return self.query_one("#status-agent", AgentStatus)
        except Exception:
            return None

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
        runtime = getattr(self.state, "agent_runtime", None)
        if runtime is not None:
            try:
                runtime.stop(grace=1.0)
            except Exception:  # pragma: no cover
                pass
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

    def show_page(
        self,
        page: PageId,
        push_history: bool = True,
        focus_content: bool = True,
    ) -> None:
        if page is self.state.active_page:
            if focus_content:
                self._focus_page(page)
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
            if focus_content:
                self._focus_page(page)
            if page is PageId.EVIDENCE and not self.state.evidence_sessions:
                target._scan()  # type: ignore[attr-defined]
        except Exception:  # pragma: no cover
            pass
        self.request_snapshot(SNAPSHOT_NAMES[page])

    def _focus_page(self, page: PageId) -> None:
        """Move focus into the page content after a tab switch."""

        try:
            target = self.page_for(page)
        except Exception:  # pragma: no cover - page not mounted yet
            return
        self.call_after_refresh(target.focus_primary)
        self.set_timer(0.02, target.focus_primary)

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
        # No drill-down history: hand focus back to the tab strip so the
        # operator can keep switching pages with Left/Right.
        try:
            self.query_one(NavBar).focus()
        except Exception:  # pragma: no cover - initial mount
            pass

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
        self.show_page(
            message.page,
            push_history=message.push_history,
            focus_content=message.focus_content,
        )

    def on_hotkeys_changed(self, message: HotkeysChanged) -> None:
        if message.page is not None and message.page is not self.state.active_page:
            return
        if isinstance(self.app.focused, NavBar):
            # A page repaint must not replace the tab-strip hints while the
            # operator is still navigating with the arrow keys.
            self._update_hotkeys()
            return
        self.query_one(HotkeyBar).update_hints(message.hints, self.app.theme)

    def on_breadcrumb_changed(self, message: BreadcrumbChanged) -> None:
        if message.page is not None and message.page is not self.state.active_page:
            return
        self.query_one(Breadcrumb).update_items(message.items)

    def on_toast(self, message: Toast) -> None:
        self.state.last_message = message.text
        self._push_message(message.text, message.level)

    def _drain_messages(self) -> None:
        lines = logbus.buffer().drain()
        if not lines:
            return
        try:
            self.query_one(MessagePanel).extend(lines)
        except Exception:  # pragma: no cover - panel may be unmounting
            pass

    def _push_message(self, text: str, level: str = "info") -> None:
        try:
            self.query_one(MessagePanel).push(text, level)
        except Exception:  # pragma: no cover - panel may be unmounting
            pass

    def on_data_refreshed(self, message: DataRefreshed) -> None:
        self._update_chrome()
        if not self._initial_snapshot and message.scope is RefreshScope.ALL:
            self._initial_snapshot = True
            self.request_snapshot("10-dashboard")

    def on_task_state_changed(self, message: TaskStateChanged) -> None:
        if message.log_line:
            self._push_message(message.log_line, "info")
        self._update_chrome()

    def on_report_status_changed(self, message: ReportStatusChanged) -> None:
        self._update_chrome()

    # -- agent --------------------------------------------------------------
    def on_agent_status_changed(self, message: AgentStatusChanged) -> None:
        widget = self._agent_status_widget()
        if widget is not None:
            try:
                widget.update_status(message.status)
            except Exception:
                pass
        self._update_chrome()

    def on_queue_length_changed(self, message: QueueLengthChanged) -> None:
        self._update_chrome()

    def on_task_finished(self, message: TaskFinished) -> None:
        level = "info" if message.status == "finished" else "warn"
        self._push_message(
            f"远程任务 {message.remote_task_id}: {message.status}"
            + (f" ({message.error_code})" if message.error_code else ""),
            level,
        )
        self._update_chrome()

    def on_remote_task_confirm_requested(self, message: RemoteTaskConfirmRequested) -> None:
        request = message.request
        if getattr(self.app, "screen", None) is self:
            self._push_remote_confirm(request)
            return
        self._pending_confirm = request
        self._pending_confirm_since = time.monotonic()
        self.toast("远程任务确认等待当前界面关闭", "warn")
        self.set_timer(1.0, self._maybe_push_confirm)

    def _maybe_push_confirm(self) -> None:
        request = getattr(self, "_pending_confirm", None)
        if request is None:
            return
        if request.done.is_set():
            self._pending_confirm = None
            return
        since = getattr(self, "_pending_confirm_since", time.monotonic())
        if time.monotonic() - since > max(1, request.timeout_seconds):
            request.decision = ConfirmDecision(
                approved=False,
                operator=request.default_operator,
                reason="confirm_timeout",
            )
            request.done.set()
            self._pending_confirm = None
            return
        if getattr(self.app, "screen", None) is self:
            self._pending_confirm = None
            self._push_remote_confirm(request)
            return
        self.set_timer(1.0, self._maybe_push_confirm)

    def _push_remote_confirm(self, request) -> None:
        if request.done.is_set():
            return

        def _done(decision) -> None:
            if decision is None:
                decision = ConfirmDecision(
                    approved=False,
                    operator=request.default_operator,
                    reason="confirm_rejected",
                )
            request.decision = decision
            request.done.set()

        try:
            self.app.push_screen(RemoteTaskConfirmScreen(request), _done)
        except Exception:
            request.decision = ConfirmDecision(
                approved=False,
                operator=request.default_operator,
                reason="confirm_rejected",
            )
            request.done.set()

    # -- chrome -------------------------------------------------------------
    def _task_label(self) -> str:
        """Compact description of the active task for the breadcrumb."""

        task_id = self.state.active_task_id
        if not task_id:
            return ""
        task = next((item for item in self.state.task_queue if item.id == task_id), None)
        if task is None:
            return "task running"
        if self.state.cancel_event.is_set():
            return f"⚠ {task.kind.value} cancelling"
        progress = task.progress_text
        suffix = f" {progress}" if progress and progress != "-" else ""
        return f"{task.status_icon} {task.kind.value}{suffix}"

    def _nav_hotkeys(self) -> List[HotkeyHint]:
        return [
            HotkeyHint("←/→", "switch"),
            HotkeyHint("Enter", "open"),
            HotkeyHint("Ctrl+Q", "quit"),
        ]

    def _update_hotkeys(self) -> None:
        try:
            focused = self.app.focused
            hints = self._nav_hotkeys() if isinstance(focused, NavBar) else self.current_page().hotkeys()
            self.query_one(HotkeyBar).update_hints(hints, self.app.theme)
        except Exception:  # pragma: no cover - initial compose
            pass

    def on_descendant_focus(self, event) -> None:
        super().on_descendant_focus(event)
        self._update_hotkeys()

    def _update_chrome(self) -> None:
        try:
            self.query_one(StatusBar).update_state(self.state)
            breadcrumb = self.query_one(Breadcrumb)
            breadcrumb.update_items(self.state.breadcrumb_items())
            breadcrumb.set_task_label(self._task_label())
            self.query_one(NavBar).set_active(self.state.active_page)
            self._update_hotkeys()
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
            task = next(
                (item for item in self.state.task_queue if item.id == self.state.active_task_id),
                None,
            )
            title = task.title if task is not None else "a task"
            self.toast(f"{title} is running - F5 disabled; Ctrl+Q cancels", "warn")
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
