"""心跳线程与连接状态驱动。"""

from __future__ import annotations

import logging
import socket
import threading
from typing import Any, Dict, Optional

from .client import AgentClient, Response
from .clock import Clock, SystemClock, parse_rfc3339, rfc3339
from .config import AgentConfig
from .models import ALL_TASK_TYPES
from .snapshot import HostSnapshot, SnapshotProvider
from .state import StateStore
from .version import AGENT_VERSION, PROTOCOL_VERSION

log = logging.getLogger(__name__)


class HeartbeatLoop:
    def __init__(
        self,
        client: AgentClient,
        snapshot: SnapshotProvider,
        store: StateStore,
        runtime: Any,
        cfg: AgentConfig,
        clock: Optional[Clock] = None,
    ) -> None:
        self.client = client
        self.snapshot = snapshot
        self.store = store
        self.runtime = runtime
        self.cfg = cfg
        self.clock = clock or SystemClock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._failures = 0

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="agent-heartbeat", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    # -- loop ---------------------------------------------------------------
    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.beat_once()
            except Exception as exc:  # pragma: no cover - 循环不得退出
                log.exception("心跳循环异常: %s", exc)
                self._on_failure()
            interval = self.runtime.heartbeat_interval()
            if self._stop.wait(max(1.0, float(interval))):
                break

    def beat_once(self) -> bool:
        try:
            snapshot = self.snapshot.capture(fresh=False)
        except Exception as exc:
            log.warning("心跳快照失败: %s", exc)
            snapshot = None
        payload = self.build_payload(snapshot)
        response = self.client.heartbeat(payload)
        return self._handle(response)

    # -- payload ------------------------------------------------------------
    def build_payload(self, snapshot: Optional[HostSnapshot]) -> Dict[str, Any]:
        runtime = self.runtime
        server_time_delta = runtime.server_time_delta()
        payload: Dict[str, Any] = {
            "schema_version": 1,
            "agent_id": self.cfg.agent_id,
            "agent_type": "raidforge",
            "agent_version": AGENT_VERSION,
            "protocol_version": PROTOCOL_VERSION,
            "server_sn": runtime.server_sn,
            "hostname": socket.gethostname(),
            "status": runtime.heartbeat_status(),
            "current_task": runtime.current_task_payload(),
            "queue_depth": runtime.queue_depth(),
            "queue_max": self.cfg.queue_max,
            "capabilities": self._capabilities(snapshot),
            "hardware": snapshot.hardware if snapshot else {},
            "boot_media": snapshot.boot_media.heartbeat_payload() if snapshot else {},
            "storage": snapshot.storage if snapshot else {},
            "last_error": runtime.last_error,
            "server_time_delta_s": server_time_delta,
        }
        return payload

    def _capabilities(self, snapshot: Optional[HostSnapshot]) -> Dict[str, Any]:
        if snapshot is None:
            return {
                "task_types": ALL_TASK_TYPES,
                "dry_run": True,
                "evidence_kinds": [],
                "controllers": [],
            }
        return snapshot.heartbeat_capabilities()

    # -- response -----------------------------------------------------------
    def _handle(self, response: Response) -> bool:
        if response.ok:
            body = response.body or {}
            self._failures = 0
            self.store.mark_heartbeat(rfc3339(self.clock.utcnow()))
            self.runtime.on_heartbeat_ok(body)
            return True

        status = response.status
        code = response.error_code
        if status == 401:
            self._failures = 0
            self.runtime.on_auth_failed("认证失败：请更换令牌")
            return False
        if status == 403:
            self._failures = 0
            self.runtime.on_disabled(code or "agent_disabled")
            return False
        if status == 426:
            self._failures = 0
            self.runtime.on_protocol_too_old()
            return False
        if status == 409 and code == "server_sn_conflict":
            self.runtime.set_last_error("server_sn_conflict", "server_sn 已被其他 agent 占用")
            return False
        if status == 503:
            self._failures = 0
            self.runtime.on_maintenance()
            return False
        self._on_failure()
        return False

    def _on_failure(self) -> None:
        self._failures += 1
        self.runtime.on_heartbeat_failure(self._failures)


__all__ = ["HeartbeatLoop"]
