"""短轮询：领取任务、信封校验、只读合并与租约归还。"""

from __future__ import annotations

import logging
import random
import threading
from typing import Any, Optional

from .client import AgentClient, Response
from .clock import Clock, SystemClock
from .models import RemoteTaskType, TaskRejected
from .queue import RemoteAdmission
from .state import StateStore
from .validation import parse_task, validate_envelope

log = logging.getLogger(__name__)


class TaskPoller:
    def __init__(
        self,
        client: AgentClient,
        admission: RemoteAdmission,
        store: StateStore,
        runtime: Any,
        clock: Optional[Clock] = None,
    ) -> None:
        self.client = client
        self.admission = admission
        self.store = store
        self.runtime = runtime
        self.clock = clock or SystemClock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="agent-poller", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    # -- loop ---------------------------------------------------------------
    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception as exc:  # pragma: no cover - 循环不得退出
                log.exception("轮询异常: %s", exc)
            interval = float(self.runtime.agent_cfg.poll_interval_short)
            interval += random.uniform(0.0, float(self.runtime.agent_cfg.poll_jitter_ms) / 1000.0)
            if self._stop.wait(interval):
                break

    def poll_once(self) -> int:
        if not self.runtime.can_poll():
            return 0
        response = self.client.next_task(self.runtime.server_sn)
        if response.status == 204:
            return 0
        if not response.ok:
            self._handle_error(response)
            return 0

        body = response.body or {}
        envelope = body.get("task") if isinstance(body.get("task"), dict) else None
        if envelope is None and isinstance(body, dict) and body.get("task_id"):
            # 控制面参考实现直接返回任务信封（无 {ok, task} 包裹）。
            envelope = body
        if not isinstance(envelope, dict):
            return 0
        return self._ingest(envelope)

    # -- ingest -------------------------------------------------------------
    def _ingest(self, envelope: dict) -> int:
        try:
            task = parse_task(envelope)
            validate_envelope(task, self.runtime.server_sn, self.clock.utcnow())
        except TaskRejected as rejection:
            if rejection.error_code == "lease_expired":
                self._release_envelope(envelope, "lease_expired")
                return 0
            self.runtime.report_rejection(envelope, rejection)
            return 0
        except Exception as exc:
            log.warning("任务解析异常: %s", exc)
            self.runtime.report_rejection(
                envelope, TaskRejected("invalid_params", f"任务解析失败: {exc}")
            )
            return 0

        if task.type is RemoteTaskType.ABORT_CURRENT:
            self.runtime.handle_abort_current(task)
            return 0

        if self.store.is_executed(task.task_id, task.lease_id):
            copy = self.store.load_result_copy(task.task_id)
            if copy:
                self.runtime.reporter.deliver(copy)
            log.info("重复投递 %s，仅重发结果", task.task_id)
            return 0

        if not self.admission.can_accept(task, paused=self.runtime.paused):
            self.runtime.release_task(task, "busy")
            return 0

        outcome = self.admission.submit_remote(task, self.runtime.remote_job(task))
        if outcome.startswith("coalesced:"):
            old_task_id = outcome.split(":", 1)[1]
            self.runtime.supersede(old_task_id)
            return 1
        if outcome == "busy":
            self.runtime.release_task(task, "busy")
            return 0
        return 1

    # -- errors -------------------------------------------------------------
    def _handle_error(self, response: Response) -> None:
        status = response.status
        if status == 401:
            self.runtime.on_auth_failed("认证失败：请更换令牌")
        elif status == 403:
            self.runtime.on_disabled(response.error_code or "agent_disabled")
        elif status == 426:
            self.runtime.on_protocol_too_old()
        elif status == 503:
            self.runtime.on_maintenance()
        elif status == 429:
            retry_after = 0.0
            try:
                retry_after = float(response.headers.get("retry-after", "0"))
            except (TypeError, ValueError):
                retry_after = 0.0
            if retry_after > 0:
                self._stop.wait(min(300.0, retry_after))
        else:
            log.warning("tasks/next 失败 status=%s code=%s", status, response.error_code)

    def _release_envelope(self, envelope: dict, reason: str) -> None:
        task_id = str(envelope.get("task_id") or "")
        lease_id = str(envelope.get("lease_id") or "")
        if task_id and lease_id:
            try:
                self.client.release(task_id, lease_id, reason)
            except Exception:
                pass


__all__ = ["TaskPoller"]
