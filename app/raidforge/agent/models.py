"""Agent 领域模型与协议枚举。"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from .clock import rfc3339

SCHEMA_VERSION = 1


class RemoteTaskType(str, Enum):
    REFRESH_HARDWARE = "refresh_hardware"
    LIST_CONTROLLERS = "list_controllers"
    LIST_PDS = "list_pds"
    LIST_VDS = "list_vds"
    CREATE_RAID = "create_raid"
    DELETE_RAID = "delete_raid"
    CLEAR_CONFIG = "clear_config"
    ERASE = "erase_disks"
    ABORT_CURRENT = "abort_current"

    @property
    def destructive(self) -> bool:
        return self in _DESTRUCTIVE

    @property
    def read_only(self) -> bool:
        return self in _READ_ONLY


_DESTRUCTIVE = {
    RemoteTaskType.CREATE_RAID,
    RemoteTaskType.DELETE_RAID,
    RemoteTaskType.CLEAR_CONFIG,
    RemoteTaskType.ERASE,
}
_READ_ONLY = {
    RemoteTaskType.REFRESH_HARDWARE,
    RemoteTaskType.LIST_CONTROLLERS,
    RemoteTaskType.LIST_PDS,
    RemoteTaskType.LIST_VDS,
}
ALL_TASK_TYPES = sorted(task.value for task in RemoteTaskType)


class TaskPhase(str, Enum):
    RECEIVED = "received"
    VALIDATED = "validated"
    QUEUED = "queued"
    CONFIRM_PENDING = "confirm_pending"
    PRECHECK = "precheck"
    RUNNING = "running"
    VERIFYING = "verifying"
    REPORTING = "reporting"
    DONE = "done"
    FAILED = "failed"
    CANCELLING = "cancelling"
    RELEASED = "released"


class ReportedStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    FINISHED = "finished"
    FAILED = "failed"


class ConnectionState(str, Enum):
    UNCONFIGURED = "unconfigured"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    OFFLINE = "offline"
    AUTH_FAILED = "auth_failed"
    DISABLED = "disabled"
    MAINTENANCE = "maintenance"
    PROTOCOL_TOO_OLD = "protocol_too_old"


class ConfirmPolicy(str, Enum):
    SERVER = "server"
    ALWAYS = "always"


class TaskRejected(Exception):
    """参数/安全校验拒绝；``error_code`` 直接进 result。"""

    def __init__(
        self,
        error_code: str,
        message: str = "",
        *,
        field: str = "",
        detail: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message or error_code)
        self.error_code = error_code
        self.message = message or error_code
        self.field = field
        self.detail = dict(detail or {})


@dataclass
class RemoteTask:
    task_id: str
    type: RemoteTaskType
    server_sn: str
    params: Dict[str, Any]
    require_local_confirm: bool = True
    timeout_seconds: int = 600
    lease_id: str = ""
    lease_expires_at: str = ""
    created_by: str = ""
    created_at: str = ""
    expires_at: str = ""
    priority: int = 100
    schema_version: int = SCHEMA_VERSION
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def destructive(self) -> bool:
        return self.type.destructive

    @property
    def read_only(self) -> bool:
        return self.type.read_only

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "type": self.type.value,
            "server_sn": self.server_sn,
            "params": dict(self.params),
            "require_local_confirm": self.require_local_confirm,
            "timeout_seconds": self.timeout_seconds,
            "lease_id": self.lease_id,
            "lease_expires_at": self.lease_expires_at,
            "created_by": self.created_by,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "priority": self.priority,
            "schema_version": self.schema_version,
        }


@dataclass
class ConfirmRequest:
    """交给远程确认屏的数据。"""

    task_id: str
    task_type: str
    server_sn: str
    created_by: str
    summary: List[str]
    targets: List[str]
    commands: List[str]
    topology_hash: str
    timeout_seconds: int
    default_operator: str = ""
    policy_override_ignored: bool = False
    evidence_dir: str = ""
    done: threading.Event = field(default_factory=threading.Event, repr=False)
    decision: Optional["ConfirmDecision"] = None
    requested_at: str = field(default_factory=rfc3339)


@dataclass
class ConfirmDecision:
    approved: bool
    operator: str = ""
    reason: str = ""
    at: str = field(default_factory=rfc3339)

    @property
    def rejected_code(self) -> str:
        return "confirm_timeout" if self.reason == "confirm_timeout" else "confirm_rejected"


@dataclass
class CommandRecord:
    seq: int
    argv: List[str]
    exit_code: Optional[int]
    started_at: str
    duration_ms: int
    stdout_sha256: str = ""
    stderr_tail: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "seq": self.seq,
            "argv": list(self.argv),
            "exit_code": self.exit_code,
            "started_at": self.started_at,
            "duration_ms": self.duration_ms,
            "stdout_sha256": self.stdout_sha256,
            "stderr_tail": self.stderr_tail,
        }


@dataclass
class EvidenceRecord:
    filename: str
    size: int
    sha256: str
    kind: str
    content_type: str
    stage: str
    collected_at: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "filename": self.filename,
            "size": self.size,
            "sha256": self.sha256,
            "kind": self.kind,
            "content_type": self.content_type,
            "stage": self.stage,
            "collected_at": self.collected_at,
        }


@dataclass
class AgentResult:
    task_id: str
    agent_id: str
    lease_id: str
    status: ReportedStatus = ReportedStatus.FAILED
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    error_detail: Dict[str, Any] = field(default_factory=dict)
    started_at: str = ""
    finished_at: str = ""
    duration_ms: int = 0
    topology_hash_before: str = ""
    topology_hash_after: str = ""
    confirm: Dict[str, Any] = field(default_factory=dict)
    result: Dict[str, Any] = field(default_factory=dict)
    commands: List[CommandRecord] = field(default_factory=list)
    evidence_status: str = "pending"
    evidence_dir: str = ""
    evidence_manifest: List[EvidenceRecord] = field(default_factory=list)
    evidence_incomplete: bool = False
    terminal_state_unknown: bool = False
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "task_id": self.task_id,
            "agent_id": self.agent_id,
            "lease_id": self.lease_id,
            "status": self.status.value,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "error_detail": dict(self.error_detail),
            "started_at": self.started_at or None,
            "finished_at": self.finished_at or None,
            "duration_ms": int(self.duration_ms),
            "topology_hash_before": self.topology_hash_before or None,
            "topology_hash_after": self.topology_hash_after or None,
            "confirm": dict(self.confirm) or None,
            "result": dict(self.result),
            "commands": [command.to_dict() for command in self.commands],
            "evidence": {
                "status": self.evidence_status,
                "dir": self.evidence_dir,
                "manifest": [record.to_dict() for record in self.evidence_manifest],
            },
            "evidence_incomplete": bool(self.evidence_incomplete),
            "terminal_state_unknown": bool(self.terminal_state_unknown),
            "warnings": list(self.warnings),
        }

    def fail(self, error_code: str, message: str = "", detail: Optional[Dict[str, Any]] = None) -> None:
        self.status = ReportedStatus.FAILED
        self.error_code = error_code
        self.error_message = message or error_code
        if detail:
            self.error_detail.update(detail)


@dataclass
class AgentStatusSnapshot:
    connection: ConnectionState = ConnectionState.CONNECTING
    text: str = "连接中"
    queue_depth: int = 0
    queue_max: int = 4
    paused: bool = False
    current_task: str = ""
    current_type: str = ""
    current_minutes: int = 0
    note: str = ""
    last_error: str = ""

    def render(self) -> str:
        if self.paused:
            head = "AGT‖ 已暂停"
        elif self.connection is ConnectionState.CONNECTED:
            head = "AGT●"
        elif self.connection is ConnectionState.UNCONFIGURED:
            head = "AGT○ 未配置"
        elif self.connection in (ConnectionState.AUTH_FAILED, ConnectionState.DISABLED):
            head = "AGT✕"
        elif self.connection is ConnectionState.PROTOCOL_TOO_OLD:
            head = "AGT!"
        else:
            head = "AGT○"
        parts = [head]
        if self.connection is not ConnectionState.UNCONFIGURED:
            parts.append(f"队列:{self.queue_depth}")
        if self.current_task:
            parts.append(f"{self.current_type} 已运行 {self.current_minutes} 分")
        if self.note:
            parts.append(self.note)
        return " ".join(parts)


@dataclass
class AgentLimits:
    timeout_seconds_max: int = 7200
    max_files: int = 32
    max_file_bytes: int = 64 * 1024 * 1024
    max_total_bytes: int = 256 * 1024 * 1024


__all__ = [
    "ALL_TASK_TYPES",
    "AgentLimits",
    "AgentResult",
    "AgentStatusSnapshot",
    "CommandRecord",
    "ConfirmDecision",
    "ConfirmPolicy",
    "ConfirmRequest",
    "ConnectionState",
    "EvidenceRecord",
    "RemoteTask",
    "RemoteTaskType",
    "ReportedStatus",
    "SCHEMA_VERSION",
    "TaskPhase",
    "TaskRejected",
]
