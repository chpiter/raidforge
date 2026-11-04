"""Agent 持久化状态与原子写。

``agent_state.json`` 是唯一状态文件；pending result 载荷放在 ``pending/``，
避免状态文件膨胀。所有写入都是 tmp → fsync → os.replace。
"""

from __future__ import annotations

import copy
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple, TypeVar

from .clock import Clock, SystemClock, rfc3339

log = logging.getLogger(__name__)

T = TypeVar("T")
STATE_SCHEMA = "raidforge-agent-state@1"
MAX_RECENT = 50
MAX_EXECUTED = 220


def atomic_write_bytes(path: Path, data: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.tmp-{os.getpid()}-{threading.get_ident()}")
    try:
        fd = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        try:
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(str(temp), str(path))
        try:
            dir_fd = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
    finally:
        try:
            if temp.exists():
                temp.unlink()
        except OSError:
            pass


def atomic_write_json(path: Path, payload: Any, mode: int = 0o600) -> None:
    data = json.dumps(payload, ensure_ascii=False, indent=2, default=str).encode("utf-8")
    atomic_write_bytes(path, data, mode=mode)


@dataclass
class AgentState:
    schema_version: int = 1
    server_sn: str = ""
    agent_id: str = ""
    last_heartbeat_success: str = ""
    current_task: Optional[Dict[str, Any]] = None
    recent_tasks: List[Dict[str, Any]] = field(default_factory=list)
    pending_results: List[str] = field(default_factory=list)
    pending_evidence: List[str] = field(default_factory=list)
    executed_task_ids: List[str] = field(default_factory=list)
    queue: Dict[str, Any] = field(default_factory=lambda: {"paused": False, "pause_reason": ""})

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "server_sn": self.server_sn,
            "agent_id": self.agent_id,
            "last_heartbeat_success": self.last_heartbeat_success,
            "current_task": copy.deepcopy(self.current_task),
            "recent_tasks": copy.deepcopy(self.recent_tasks),
            "pending_results": list(self.pending_results),
            "pending_evidence": list(self.pending_evidence),
            "executed_task_ids": list(self.executed_task_ids),
            "queue": dict(self.queue),
        }

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "AgentState":
        state = cls()
        state.schema_version = int(payload.get("schema_version", 1) or 1)
        state.server_sn = str(payload.get("server_sn") or "")
        state.agent_id = str(payload.get("agent_id") or "")
        state.last_heartbeat_success = str(payload.get("last_heartbeat_success") or "")
        current = payload.get("current_task")
        state.current_task = dict(current) if isinstance(current, dict) else None
        state.recent_tasks = [dict(item) for item in payload.get("recent_tasks") or [] if isinstance(item, dict)]
        state.pending_results = [str(item) for item in payload.get("pending_results") or []]
        state.pending_evidence = [str(item) for item in payload.get("pending_evidence") or []]
        state.executed_task_ids = [str(item) for item in payload.get("executed_task_ids") or []]
        queue = payload.get("queue")
        state.queue = dict(queue) if isinstance(queue, dict) else {"paused": False, "pause_reason": ""}
        return state


class StateStore:
    """单进程、线程安全的持久化存储。"""

    def __init__(self, root: Path, clock: Optional[Clock] = None) -> None:
        self.root = Path(root)
        self.pending_dir = self.root / "pending"
        self.results_dir = self.root / "results"
        self.state_path = self.root / "agent_state.json"
        self.lock_path = self.root / "agent.lock"
        self.clock = clock or SystemClock()
        self._lock = threading.RLock()
        self._state: Optional[AgentState] = None
        self._lock_fd: Optional[int] = None
        self._dirs_ready = False

    def _ensure_dirs(self) -> None:
        if self._dirs_ready:
            return
        self.root.mkdir(parents=True, exist_ok=True)
        self.pending_dir.mkdir(parents=True, exist_ok=True)
        self.results_dir.mkdir(parents=True, exist_ok=True)
        self._dirs_ready = True

    # -- lifecycle ----------------------------------------------------------
    def load(self) -> AgentState:
        with self._lock:
            self._ensure_dirs()
            if self._state is not None:
                return self._state
            if not self.state_path.exists():
                self._state = AgentState()
                return self._state
            try:
                payload = json.loads(self.state_path.read_text(encoding="utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("state root is not an object")
                self._state = AgentState.from_dict(payload)
            except Exception as exc:
                stamp = self.clock.utcnow().strftime("%Y%m%dT%H%M%SZ")
                quarantine = self.root / f"agent_state.corrupt-{stamp}.json"
                log.error("状态文件损坏，隔离为 %s: %s", quarantine, exc)
                try:
                    self.state_path.replace(quarantine)
                except OSError:
                    pass
                self._state = AgentState()
            return self._state

    def snapshot(self) -> AgentState:
        with self._lock:
            return copy.deepcopy(self.load())

    def mutate(self, fn: Callable[[AgentState], T]) -> T:
        with self._lock:
            state = self.load()
            result = fn(state)
            self._save_locked()
            return result

    def _save_locked(self) -> None:
        if self._state is not None:
            atomic_write_json(self.state_path, self._state.to_dict())

    # -- identity / heartbeat ----------------------------------------------
    def set_identity(self, server_sn: str, agent_id: str) -> None:
        def _apply(state: AgentState) -> None:
            state.server_sn = server_sn
            state.agent_id = agent_id

        self.mutate(_apply)

    def mark_heartbeat(self, at: str) -> None:
        self.mutate(lambda state: setattr(state, "last_heartbeat_success", at))

    # -- current task / queue ----------------------------------------------
    def set_current_task(self, record: Optional[Dict[str, Any]]) -> None:
        self.mutate(lambda state: setattr(state, "current_task", copy.deepcopy(record)))

    def set_paused(self, paused: bool, reason: str = "") -> None:
        def _apply(state: AgentState) -> None:
            state.queue = {"paused": bool(paused), "pause_reason": reason if paused else ""}

        self.mutate(_apply)

    def is_paused(self) -> Tuple[bool, str]:
        with self._lock:
            state = self.load()
            queue = state.queue or {}
            return bool(queue.get("paused")), str(queue.get("pause_reason") or "")

    # -- idempotency --------------------------------------------------------
    def mark_executed(self, task_id: str, lease_id: str) -> bool:
        key = f"{task_id}:{lease_id}"

        def _apply(state: AgentState) -> bool:
            if key in state.executed_task_ids:
                return False
            state.executed_task_ids.append(key)
            state.executed_task_ids = state.executed_task_ids[-MAX_EXECUTED:]
            return True

        return self.mutate(_apply)

    def is_executed(self, task_id: str, lease_id: str) -> bool:
        with self._lock:
            return f"{task_id}:{lease_id}" in self.load().executed_task_ids

    # -- recent -------------------------------------------------------------
    def remember_task(self, record: Dict[str, Any]) -> None:
        def _apply(state: AgentState) -> None:
            state.recent_tasks.append(copy.deepcopy(record))
            state.recent_tasks = state.recent_tasks[-MAX_RECENT:]

        self.mutate(_apply)

    # -- pending results ----------------------------------------------------
    def _pending_path(self, task_id: str) -> Path:
        safe = "".join(char for char in task_id if char.isalnum() or char in "._-")
        return self.pending_dir / f"result_{safe}.json"

    def put_pending_result(self, payload: Dict[str, Any]) -> Path:
        self._ensure_dirs()
        task_id = str(payload.get("task_id") or "")
        target = self._pending_path(task_id)
        atomic_write_json(target, payload)

        def _apply(state: AgentState) -> None:
            if task_id and task_id not in state.pending_results:
                state.pending_results.append(task_id)

        self.mutate(_apply)
        return target

    def iter_pending_results(self) -> Iterator[Tuple[Path, Dict[str, Any]]]:
        self._ensure_dirs()
        with self._lock:
            task_ids = list(self.load().pending_results)
        for task_id in sorted(task_ids):
            path = self._pending_path(task_id)
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if isinstance(payload, dict):
                yield path, payload

    def drop_pending_result(self, task_id: str, lease_id: str = "") -> None:
        path = self._pending_path(task_id)
        try:
            if path.exists():
                path.unlink()
        except OSError:
            pass

        def _apply(state: AgentState) -> None:
            state.pending_results = [item for item in state.pending_results if item != task_id]
            if lease_id:
                state.executed_task_ids = [
                    item for item in state.executed_task_ids if item != f"{task_id}:{lease_id}"
                ]

        self.mutate(_apply)

    # -- pending evidence ---------------------------------------------------
    def _pending_evidence_path(self, task_id: str) -> Path:
        safe = "".join(char for char in task_id if char.isalnum() or char in "._-")
        return self.pending_dir / f"evidence_{safe}.json"

    def put_pending_evidence(self, payload: Dict[str, Any]) -> Path:
        self._ensure_dirs()
        task_id = str(payload.get("task_id") or "")
        target = self._pending_evidence_path(task_id)
        atomic_write_json(target, payload)

        def _apply(state: AgentState) -> None:
            if task_id and task_id not in state.pending_evidence:
                state.pending_evidence.append(task_id)

        self.mutate(_apply)
        return target

    def iter_pending_evidence(self) -> Iterator[Tuple[Path, Dict[str, Any]]]:
        self._ensure_dirs()
        with self._lock:
            task_ids = list(self.load().pending_evidence)
        for task_id in sorted(task_ids):
            path = self._pending_evidence_path(task_id)
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if isinstance(payload, dict):
                yield path, payload

    def drop_pending_evidence(self, task_id: str) -> None:
        path = self._pending_evidence_path(task_id)
        try:
            if path.exists():
                path.unlink()
        except OSError:
            pass

        def _apply(state: AgentState) -> None:
            state.pending_evidence = [item for item in state.pending_evidence if item != task_id]

        self.mutate(_apply)

    # -- result copies (idempotent re-send) --------------------------------
    def save_result_copy(self, payload: Dict[str, Any]) -> Path:
        self._ensure_dirs()
        task_id = str(payload.get("task_id") or "")
        safe = "".join(char for char in task_id if char.isalnum() or char in "._-")
        target = self.results_dir / f"result_{safe}.json"
        atomic_write_json(target, payload)
        self._purge_result_copies()
        return target

    def load_result_copy(self, task_id: str) -> Optional[Dict[str, Any]]:
        safe = "".join(char for char in task_id if char.isalnum() or char in "._-")
        path = self.results_dir / f"result_{safe}.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
        return payload if isinstance(payload, dict) else None

    def _purge_result_copies(self, max_age_seconds: float = 7 * 24 * 3600) -> None:
        cutoff = time.time() - max_age_seconds
        try:
            for path in self.results_dir.glob("result_*.json"):
                if path.stat().st_mtime < cutoff:
                    path.unlink()
        except OSError:
            pass

    # -- single process -----------------------------------------------------
    def acquire_process_lock(self) -> bool:
        if self._lock_fd is not None:
            return True
        self._ensure_dirs()
        try:
            fd = os.open(str(self.lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            if self._lock_is_stale():
                try:
                    self.lock_path.unlink()
                except OSError:
                    return False
                return self.acquire_process_lock()
            log.warning("另一个 agent 实例正在运行（%s）", self.lock_path)
            return False
        except OSError as exc:
            log.warning("无法创建 agent 进程锁: %s", exc)
            return False
        try:
            os.write(fd, f"{os.getpid()}\n".encode())
            os.fsync(fd)
        except OSError:
            pass
        self._lock_fd = fd
        return True

    def _lock_is_stale(self) -> bool:
        try:
            age = time.time() - self.lock_path.stat().st_mtime
            if age > 3600:
                return True
            text = self.lock_path.read_text(encoding="utf-8", errors="replace").strip()
            pid = int(text.splitlines()[0]) if text else 0
        except (OSError, ValueError):
            return True
        if pid <= 0:
            return True
        try:
            os.kill(pid, 0)
        except OSError:
            return True
        return False

    def release_process_lock(self) -> None:
        if self._lock_fd is not None:
            try:
                os.close(self._lock_fd)
            except OSError:
                pass
            self._lock_fd = None
        try:
            if self.lock_path.exists():
                self.lock_path.unlink()
        except OSError:
            pass


__all__ = [
    "AgentState",
    "STATE_SCHEMA",
    "StateStore",
    "atomic_write_bytes",
    "atomic_write_json",
    "rfc3339",
]
