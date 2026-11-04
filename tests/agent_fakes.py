"""Agent 测试夹具：无网络、无硬件、无真实 sleep。"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

from raidforge.agent.client import Response
from raidforge.core.utils import CmdResult


class FakeClock:
    def __init__(self, start: Optional[datetime] = None) -> None:
        self.now = start or datetime(2026, 11, 4, 10, 0, 0, tzinfo=timezone.utc)
        self.mono = 1000.0
        self.sleeps: List[float] = []

    def utcnow(self) -> datetime:
        return self.now

    def monotonic(self) -> float:
        return self.mono

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.advance(seconds)

    def advance(self, seconds: float) -> None:
        self.mono += seconds
        self.now += timedelta(seconds=seconds)


@dataclass
class FakeRunner:
    responses: Dict[str, CmdResult] = field(default_factory=dict)
    calls: List[List[str]] = field(default_factory=list)

    def run(self, argv, timeout: float = 10.0) -> CmdResult:
        self.calls.append(list(argv))
        key = str(argv[0]) if argv else ""
        if key in self.responses:
            result = self.responses[key]
            if callable(result):
                result = result(argv)
            return result
        return CmdResult(return_code=127, stdout="", stderr="not found", duration=0.0, argv=list(argv))


class FakeTransport:
    def __init__(self, script: Optional[List[Any]] = None) -> None:
        self.script = list(script or [])
        self.calls: List[Dict[str, Any]] = []

    def push(self, response: Any) -> None:
        self.script.append(response)

    def request(self, method, url, *, headers, timeout, json_body=None, data=None, files=None):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": dict(headers),
                "timeout": timeout,
                "json": json_body,
                "data": data,
                "files": files,
            }
        )
        if not self.script:
            return Response(status=200, body={"ok": True})
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        if callable(item):
            return item(method, url)
        return item


class FakeClient:
    """按方法名脚本化响应；所有调用都记录。"""

    def __init__(self) -> None:
        self.scripts: Dict[str, List[Any]] = {}
        self.calls: List[tuple] = []
        self.default = Response(status=200, body={"ok": True})

    def push(self, name: str, response: Any) -> None:
        self.scripts.setdefault(name, []).append(response)

    def _call(self, name: str, *args, **kwargs) -> Response:
        self.calls.append((name, args, kwargs))
        items = self.scripts.get(name)
        if not items:
            return self.default
        item = items.pop(0)
        if callable(item):
            return item(*args, **kwargs)
        if isinstance(item, Exception):
            raise item
        return item

    def heartbeat(self, payload):
        return self._call("heartbeat", payload)

    def next_task(self, server_sn):
        return self._call("next_task", server_sn)

    def get_task(self, task_id):
        return self._call("get_task", task_id)

    def started(self, task_id, lease_id):
        return self._call("started", task_id, lease_id)

    def progress(self, task_id, lease_id, payload):
        return self._call("progress", task_id, lease_id, payload)

    def cancel_requested(self, task_id):
        return self._call("cancel_requested", task_id)

    def result(self, task_id, payload, attempts=1):
        return self._call("result", task_id, payload, attempts)

    def evidence(self, task_id, lease_id, record, data, manifest_index=0):
        return self._call("evidence", task_id, lease_id, record, data, manifest_index)

    def release(self, task_id, lease_id, reason=""):
        return self._call("release", task_id, lease_id, reason)

    def scrub(self, text):
        return text


class FakeAdmission:
    def __init__(self, accept: bool = True, outcome: str = "accepted") -> None:
        self.accept = accept
        self.outcome = outcome
        self.submitted: List[Any] = []
        self.released: List[str] = []

    def can_poll(self, paused: bool = False) -> bool:
        return True

    def can_accept(self, task, paused: bool = False) -> bool:
        return self.accept

    def submit_remote(self, task, job) -> str:
        self.submitted.append((task, job))
        return self.outcome

    def remote_queued(self) -> int:
        return len(self.submitted)

    def mark_running(self, task_id):
        return None

    def mark_finished(self, task_id):
        return None

    def clear_unstarted(self):
        return []


class FakeStore:
    def __init__(self) -> None:
        self.heartbeats: List[str] = []
        self.executed: set = set()

    def mark_heartbeat(self, at: str) -> None:
        self.heartbeats.append(at)

    def is_executed(self, task_id: str, lease_id: str) -> bool:
        return f"{task_id}:{lease_id}" in self.executed

    def load_result_copy(self, task_id: str):
        return None


def make_envelope(
    task_id: str = "tsk_01JB8Z4Q2M",
    lease_id: str = "lse_7f3c9a1b",
    task_type: str = "refresh_hardware",
    **overrides,
) -> Dict[str, Any]:
    payload = {
        "schema_version": 1,
        "task_id": task_id,
        "type": task_type,
        "server_sn": "SN12345678",
        "params": {},
        "require_local_confirm": True,
        "timeout_seconds": 600,
        "lease_id": lease_id,
        "lease_expires_at": "2026-11-04T11:05:00Z",
        "created_by": "alice",
        "created_at": "2026-11-04T10:00:00Z",
        "expires_at": "2026-11-05T10:00:00Z",
        "priority": 100,
    }
    payload.update(overrides)
    return payload


def wait_for(predicate: Callable[[], bool], timeout: float = 3.0) -> bool:
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()
