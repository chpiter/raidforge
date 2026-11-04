"""状态栏中的 Agent 状态片段。"""

from __future__ import annotations

from textual.widgets import Static

from ...agent.models import AgentStatusSnapshot


class AgentStatus(Static):
    """挂在 ``#status-bar`` 内的单行状态；不新增 chrome 行。"""

    def __init__(self, status: AgentStatusSnapshot | None = None, **kwargs) -> None:
        super().__init__("", **kwargs)
        self._status = status or AgentStatusSnapshot()

    def on_mount(self) -> None:
        self.styles.width = "auto"
        self.styles.padding = (0, 1)
        self.update_status(self._status)

    def update_status(self, status: AgentStatusSnapshot) -> None:
        self._status = status
        self.update(status.render())


__all__ = ["AgentStatus"]
