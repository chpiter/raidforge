"""结果落盘与无限补传。"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

from .client import AgentClient, Response
from .clock import Clock, SystemClock
from .state import StateStore

log = logging.getLogger(__name__)

RESULT_DELAYS = (30.0, 60.0, 300.0)


@dataclass
class _Retry:
    attempts: int = 0
    next_at: float = 0.0


class ResultReporter:
    def __init__(
        self,
        client: AgentClient,
        store: StateStore,
        clock: Optional[Clock] = None,
    ) -> None:
        self.client = client
        self.store = store
        self.clock = clock or SystemClock()
        self._retry: Dict[str, _Retry] = {}

    # -- persistence --------------------------------------------------------
    def persist(self, payload: Dict[str, Any]) -> str:
        task_id = str(payload.get("task_id") or "")
        self.store.save_result_copy(payload)
        self.store.put_pending_result(payload)
        return task_id

    def send_once(self, payload: Dict[str, Any]) -> Response:
        return self.client.result(str(payload.get("task_id") or ""), payload, attempts=1)

    def deliver(self, payload: Dict[str, Any]) -> bool:
        """立即尝试一次；失败则保留 pending 由补传线程继续。"""

        task_id = str(payload.get("task_id") or "")
        lease_id = str(payload.get("lease_id") or "")
        self.persist(payload)
        response = self.send_once(payload)
        if self._handle(response, task_id, lease_id):
            return True
        if response.retryable:
            self._retry[task_id] = _Retry(attempts=1, next_at=self.clock.monotonic() + RESULT_DELAYS[0])
        return False

    # -- loop ---------------------------------------------------------------
    def drain_once(self, limit: int = 10) -> int:
        sent = 0
        now = self.clock.monotonic()
        for path, payload in self.store.iter_pending_results():
            if sent >= limit:
                break
            task_id = str(payload.get("task_id") or "")
            lease_id = str(payload.get("lease_id") or "")
            retry = self._retry.setdefault(task_id, _Retry())
            if retry.next_at > now:
                continue
            response = self.send_once(payload)
            if self._handle(response, task_id, lease_id):
                sent += 1
            elif response.retryable:
                retry.attempts += 1
                delay = RESULT_DELAYS[min(retry.attempts - 1, len(RESULT_DELAYS) - 1)]
                retry.next_at = self.clock.monotonic() + delay
                log.warning("result %s 补传失败，%.0fs 后重试", task_id, delay)
            else:
                retry.next_at = self.clock.monotonic() + 300.0
        return sent

    def pending_count(self) -> int:
        return len(list(self.store.iter_pending_results()))

    # -- evidence status update --------------------------------------------
    def update_evidence(
        self,
        task_id: str,
        status: str,
        manifest: list,
        incomplete: bool,
    ) -> None:
        payload = self.store.load_result_copy(task_id)
        if not payload:
            return
        evidence = payload.setdefault("evidence", {})
        evidence["status"] = status
        evidence["manifest"] = manifest
        payload["evidence_incomplete"] = bool(incomplete)
        self.store.save_result_copy(payload)

        for path, pending in self.store.iter_pending_results():
            if str(pending.get("task_id")) != task_id:
                continue
            pending.setdefault("evidence", {})["status"] = status
            pending["evidence"]["manifest"] = manifest
            pending["evidence_incomplete"] = bool(incomplete)
            try:
                from .state import atomic_write_json

                atomic_write_json(path, pending)
            except Exception:
                pass
            break

    # -- internals ----------------------------------------------------------
    def _handle(self, response: Response, task_id: str, lease_id: str) -> bool:
        if response.ok:
            self.store.drop_pending_result(task_id, lease_id)
            self._retry.pop(task_id, None)
            return True
        status = response.status
        code = response.error_code
        if status in (404, 410):
            self.store.drop_pending_result(task_id, lease_id)
            self._retry.pop(task_id, None)
            log.warning("result %s 已不存在（%s），停止补传", task_id, status)
            return True
        if status == 409:
            self.store.drop_pending_result(task_id, lease_id)
            self._retry.pop(task_id, None)
            log.warning("result %s 冲突 %s，按终态处理", task_id, code)
            return True
        if status in (400, 413):
            self.store.drop_pending_result(task_id, lease_id)
            self._retry.pop(task_id, None)
            log.warning("result %s 被永久拒绝 status=%s code=%s", task_id, status, code)
            return True
        if status in (401, 403, 426):
            log.warning("result %s 暂不可上报 status=%s code=%s", task_id, status, code)
            return False
        return False


__all__ = ["RESULT_DELAYS", "ResultReporter"]
