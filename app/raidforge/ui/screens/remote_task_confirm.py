"""远程破坏性任务确认屏（严格 ``yes``）。"""

from __future__ import annotations

import time

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Static

from ...agent.models import ConfirmDecision, ConfirmRequest


class RemoteTaskConfirmScreen(ModalScreen[ConfirmDecision]):
    """显示目标、dry-run 命令、拓扑哈希与倒计时。"""

    BINDINGS = [
        Binding("escape", "reject", "拒绝", priority=True),
        Binding("q", "reject", "拒绝", show=False),
    ]

    CSS = """
    RemoteTaskConfirmScreen {
        align: center middle;
    }
    #remote-confirm-dialog {
        width: 94%;
        height: 92%;
        border: thick $warning;
        background: $surface;
        padding: 1 2;
    }
    #remote-confirm-title {
        text-style: bold;
        color: $warning;
    }
    #remote-confirm-scroll {
        height: 1fr;
        border: round $panel;
        padding: 0 1;
    }
    #remote-confirm-targets,
    #remote-confirm-commands {
        height: auto;
    }
    #remote-confirm-error {
        color: $error;
        height: auto;
    }
    .remote-confirm-input {
        margin-top: 1;
    }
    #remote-confirm-actions {
        height: auto;
        align-horizontal: right;
    }
    """

    def __init__(self, request: ConfirmRequest) -> None:
        super().__init__()
        self.request = request
        self._deadline = time.monotonic() + max(1, int(request.timeout_seconds))
        self._timer = None

    # -- layout -------------------------------------------------------------
    def compose(self) -> ComposeResult:
        request = self.request
        with Vertical(id="remote-confirm-dialog"):
            yield Static("远程破坏性任务确认", id="remote-confirm-title")
            with VerticalScroll(id="remote-confirm-scroll"):
                yield Static(
                    "\n".join(
                        [
                            f"task_id: {request.task_id}",
                            f"type: {request.task_type}",
                            f"server_sn: {request.server_sn}",
                            f"created_by: {request.created_by}",
                            f"topology_hash: {request.topology_hash[:12]}",
                            f"剩余时间: {request.timeout_seconds}s",
                        ]
                    )
                )
                yield Static("目标：")
                yield Static("\n".join(request.targets) or "-", id="remote-confirm-targets")
                yield Static("dry-run 命令预览：")
                yield Static("\n".join(request.commands) or "-", id="remote-confirm-commands")
                if request.policy_override_ignored:
                    yield Static("服务端要求无需确认；本地策略仍然要求确认（policy_override_ignored）。")
            yield Input(
                value=request.default_operator,
                placeholder="运维者姓名（1-64 字符）",
                id="remote-confirm-operator",
                classes="remote-confirm-input",
            )
            yield Input(
                placeholder='输入 yes 确认；其他任何输入都会拒绝',
                id="remote-confirm-ack",
                classes="remote-confirm-input",
            )
            yield Static("", id="remote-confirm-error")
            with Horizontal(id="remote-confirm-actions"):
                yield Button("Execute [Enter]", id="remote-confirm-execute", variant="error")
                yield Button("Cancel [Esc]", id="remote-confirm-cancel")

    def on_mount(self) -> None:
        self._timer = self.set_interval(1.0, self._tick)
        try:
            self.query_one("#remote-confirm-operator", Input).focus()
        except Exception:
            pass
        self._tick()

    def on_unmount(self) -> None:
        if self._timer is not None:
            try:
                self._timer.stop()
            except Exception:
                pass

    # -- countdown ----------------------------------------------------------
    def _tick(self) -> None:
        remaining = int(self._deadline - time.monotonic())
        if remaining <= 0:
            self.request.decision = ConfirmDecision(
                approved=False,
                operator=self._operator(),
                reason="confirm_timeout",
            )
            self.dismiss(self.request.decision)
            return
        try:
            self.query_one("#remote-confirm-title", Static).update(
                f"远程破坏性任务确认  ·  剩余 {remaining}s"
            )
        except Exception:
            pass

    # -- actions ------------------------------------------------------------
    def action_reject(self) -> None:
        decision = ConfirmDecision(
            approved=False,
            operator=self._operator(),
            reason="confirm_rejected",
        )
        self.request.decision = decision
        self.dismiss(decision)

    def action_confirm(self) -> None:
        ack = self.query_one("#remote-confirm-ack", Input).value
        operator = self._operator()
        if not operator:
            self._error("必须填写运维者姓名")
            return
        if len(operator) > 64:
            self._error("运维者姓名最多 64 字符")
            return
        if ack != "yes":
            self._error('确认失败：必须精确输入 yes（区分大小写，不含空格）')
            try:
                self.query_one("#remote-confirm-ack", Input).value = ""
            except Exception:
                pass
            return
        decision = ConfirmDecision(approved=True, operator=operator, reason="")
        self.request.decision = decision
        self.dismiss(decision)

    def _operator(self) -> str:
        try:
            return self.query_one("#remote-confirm-operator", Input).value.strip()[:64]
        except Exception:
            return ""

    def _error(self, text: str) -> None:
        try:
            self.query_one("#remote-confirm-error", Static).update(text)
            self.app.bell()
        except Exception:
            pass

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "remote-confirm-execute":
            self.action_confirm()
        elif event.button.id == "remote-confirm-cancel":
            self.action_reject()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "remote-confirm-ack":
            self.action_confirm()


__all__ = ["RemoteTaskConfirmScreen"]
