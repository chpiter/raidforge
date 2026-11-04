"""远程任务准入与本地任务提交。

``TaskRunner`` 是唯一执行器；这里只负责容量、优先级、只读合并和登记。
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from .clock import Clock, SystemClock
from .models import RemoteTask

log = logging.getLogger(__name__)


@dataclass
class _Admitted:
    task: RemoteTask
    item: Any
    state: str = "queued"
    coalesce_key: str = ""


def coalesce_key(task: RemoteTask) -> str:
    if not task.read_only:
        return ""
    params = task.params or {}
    payload = {
        "type": task.type.value,
        "controller_ids": sorted(params.get("controller_ids") or []),
        "include_raw_output": bool(params.get("include_raw_output")),
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()[:12]
    return f"{task.type.value}:{digest}"


class RemoteAdmission:
    """限制远程任务占用：≤3 个远程任务，且同时最多 1 个破坏性任务。"""

    def __init__(
        self,
        runner: Any,
        item_factory: Callable[[RemoteTask], Any],
        *,
        clock: Optional[Clock] = None,
        queue_callback: Optional[Callable[[], None]] = None,
    ) -> None:
        self.runner = runner
        self.item_factory = item_factory
        self.clock = clock or SystemClock()
        self.queue_callback = queue_callback
        self._lock = threading.RLock()
        self._tasks: Dict[str, _Admitted] = {}

    # -- capacity -----------------------------------------------------------
    def remote_queued(self) -> int:
        with self._lock:
            return sum(1 for item in self._tasks.values() if item.state == "queued")

    def remote_total(self) -> int:
        with self._lock:
            return len(self._tasks)

    def active_lease(self) -> Optional[RemoteTask]:
        with self._lock:
            for item in self._tasks.values():
                if item.state == "running":
                    return item.task
        return None

    def destructive_outstanding(self) -> bool:
        with self._lock:
            return any(item.task.destructive for item in self._tasks.values())

    def can_poll(self, paused: bool = False) -> bool:
        if paused:
            return False
        with self._lock:
            if any(item.state == "running" for item in self._tasks.values()):
                return False
            if sum(1 for item in self._tasks.values() if item.state == "queued") >= 3:
                return False
        return True

    def can_accept(self, task: RemoteTask, paused: bool = False) -> bool:
        if paused:
            return False
        with self._lock:
            if sum(1 for item in self._tasks.values() if item.state == "queued") >= 3:
                return False
            if task.destructive and any(item.task.destructive for item in self._tasks.values()):
                return False
        return True

    # -- admission ----------------------------------------------------------
    def submit_remote(self, task: RemoteTask, job: Any) -> str:
        """返回 ``accepted`` / ``coalesced`` / ``busy``。

        ``coalesced`` 时被替换的旧任务由调用方（runtime）立即上报 superseded。
        """

        key = coalesce_key(task)
        superseded: Optional[RemoteTask] = None
        if key:
            with self._lock:
                for task_id, admitted in list(self._tasks.items()):
                    if admitted.state == "queued" and admitted.coalesce_key == key:
                        superseded = admitted.task
                        self._tasks.pop(task_id, None)
                        try:
                            admitted.item.skip_dispatch = True
                            admitted.item.status = type(admitted.item.status).CANCELLED
                        except Exception:
                            pass
                        break

        if not self.can_accept(task, paused=False):
            return "busy"

        item = self.item_factory(task)
        try:
            self.runner.submit(item, job, priority=100, origin="remote")
        except Exception as exc:
            log.warning("远程任务入队失败 %s: %s", task.task_id, exc)
            return "busy"

        with self._lock:
            self._tasks[task.task_id] = _Admitted(task=task, item=item, coalesce_key=key)
        if superseded is not None:
            self._notify_queue()
            return f"coalesced:{superseded.task_id}"
        self._notify_queue()
        return "accepted"

    def mark_running(self, task_id: str) -> None:
        with self._lock:
            admitted = self._tasks.get(task_id)
            if admitted is not None:
                admitted.state = "running"
        self._notify_queue()

    def mark_finished(self, task_id: str) -> None:
        with self._lock:
            self._tasks.pop(task_id, None)
        self._notify_queue()

    def queued_tasks(self) -> List[RemoteTask]:
        with self._lock:
            return [item.task for item in self._tasks.values() if item.state == "queued"]

    def task(self, task_id: str) -> Optional[RemoteTask]:
        with self._lock:
            admitted = self._tasks.get(task_id)
            return admitted.task if admitted else None

    def clear_unstarted(self) -> List[RemoteTask]:
        """停止时归还尚未开始的远程任务。"""

        with self._lock:
            items = [item for item in self._tasks.values() if item.state == "queued"]
            for item in items:
                self._tasks.pop(item.task.task_id, None)
                try:
                    item.item.skip_dispatch = True
                    item.item.status = type(item.item.status).CANCELLED
                except Exception:
                    pass
        if items:
            self._notify_queue()
        return [item.task for item in items]

    def _notify_queue(self) -> None:
        if self.queue_callback is not None:
            try:
                self.queue_callback()
            except Exception:
                pass


def submit_local(state: Any, task: Any, job: Any) -> Any:
    """本地任务入口：优先级 0；队列满时提示而不是静默丢弃。"""

    runner = getattr(state, "task_runner", None)
    if runner is None:
        return None
    try:
        return runner.submit(task, job, priority=0, origin="local")
    except Exception as exc:
        from ..ui.messages import Toast

        message = f"队列已满，未提交 {task.title}"
        log.warning("%s: %s", message, exc)
        try:
            state.last_message = message
            runner._safe_broadcast(Toast(message, "warn", timeout=5.0))
        except Exception:
            pass
        return None


__all__ = ["RemoteAdmission", "coalesce_key", "submit_local"]
