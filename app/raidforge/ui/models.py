"""UI-only view models.

Nothing in this module touches hardware or the evidence chain.  The models are
deliberately small, hashable where useful, and built from the existing
``core.models`` dataclasses so the driver layer does not need to know the UI
exists.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Deque, List, Optional

from ..core.models import VolumeInfo, utc_now_iso

#: Unicode status markers, in one place so every page speaks the same language.
ICON_RUNNING = "⬤"
ICON_WARNING = "⚠"
ICON_OK = "✓"
ICON_BAD = "✗"
ICON_IDLE = "○"
ICON_SELECTED = "●"


class LayoutMode(str, Enum):
    """Standard fits 80×25; Wide restores optional columns."""

    STANDARD = "standard"
    WIDE = "wide"


class PageId(str, Enum):
    """The seven fixed modules in the top navigation bar."""

    DASHBOARD = "dashboard"
    CONTROLLERS = "controllers"
    PHYSICAL = "physical"
    VIRTUAL = "virtual"
    TASKS = "tasks"
    EVIDENCE = "evidence"
    SETTINGS = "settings"

    @property
    def title(self) -> str:
        return self.value.capitalize()


class RefreshScope(str, Enum):
    """What a refresh actually changed.

    Pages only rebuild the objects named by the scope; the UI never polls and
    never reloads the world after a task.
    """

    ALL = "all"
    CONTROLLER = "controller"
    DISKS = "disks"
    EVIDENCE = "evidence"
    REPORT = "report"
    SETTINGS = "settings"
    TASKS = "tasks"


class TaskKind(str, Enum):
    REFRESH = "refresh"
    ERASE = "erase"
    RAID_CREATE = "raid_create"
    RAID_DELETE = "raid_delete"
    RAID_CLEAR = "raid_clear"
    REPORT = "report"
    UPLOAD = "upload"
    EVIDENCE_SCAN = "evidence_scan"


class TaskStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def icon(self) -> str:
        return {
            TaskStatus.QUEUED: ICON_IDLE,
            TaskStatus.RUNNING: ICON_RUNNING,
            TaskStatus.SUCCEEDED: ICON_OK,
            TaskStatus.PARTIAL: ICON_WARNING,
            TaskStatus.FAILED: ICON_BAD,
            TaskStatus.CANCELLED: ICON_WARNING,
        }[self]

    @property
    def kind(self) -> str:
        if self in (TaskStatus.SUCCEEDED,):
            return "ok"
        if self in (TaskStatus.FAILED,):
            return "bad"
        if self in (TaskStatus.PARTIAL, TaskStatus.CANCELLED):
            return "warn"
        if self is TaskStatus.RUNNING:
            return "running"
        return "idle"


class ReportState(str, Enum):
    NONE = "none"
    PENDING = "pending"
    GENERATED = "generated"
    FAILED = "failed"
    QUEUED = "queued"
    UPLOADED = "uploaded"


class StatusKind(str, Enum):
    OK = "ok"
    WARN = "warn"
    BAD = "bad"
    IDLE = "idle"
    RUNNING = "running"
    SELECTED = "selected"


@dataclass
class FilterState:
    """One page's filter values.  Stored in AppState, so it survives tab switches."""

    ctrl: str = "all"
    media: str = "all"
    state: str = "all"
    level: str = "all"
    query: str = ""

    def clear(self) -> None:
        self.ctrl = "all"
        self.media = "all"
        self.state = "all"
        self.level = "all"
        self.query = ""


@dataclass
class HotkeyHint:
    """One chip in the bottom bar."""

    key: str
    label: str
    danger: bool = False
    enabled: bool = True


@dataclass
class BreadcrumbItem:
    label: str
    page: Optional[PageId] = None


@dataclass
class RefreshState:
    active: bool = False
    scope: RefreshScope = RefreshScope.ALL
    started_at: str = ""
    finished_at: str = ""
    error: str = ""
    generation: int = 0


@dataclass
class ReportStatus:
    state: ReportState = ReportState.NONE
    channel: str = ""
    message: str = ""
    updated_at: str = ""
    queued: bool = False
    report_path: str = ""

    def as_text(self) -> str:
        if self.state is ReportState.NONE:
            return "no report yet"
        suffix = f" ({self.channel})" if self.channel else ""
        queued = " [queued locally]" if self.queued else ""
        return f"{self.state.value}{suffix}{queued}: {self.message or '-'}"


@dataclass
class EvidenceFile:
    rel_path: str
    size: int = 0
    kind: str = "file"
    mtime: str = ""


@dataclass
class EvidenceSessionView:
    session_id: str
    path: str
    started_at: str = ""
    size_bytes: int = 0
    file_count: int = 0
    manifest_ok: bool = False
    report_path: str = ""
    report_state: ReportState = ReportState.NONE
    upload: str = ""
    files: List[EvidenceFile] = field(default_factory=list)

    @property
    def size_human(self) -> str:
        value = float(self.size_bytes)
        for unit in ("B", "K", "M", "G", "T"):
            if value < 1024.0 or unit == "T":
                return f"{value:.1f}{unit}"
            value /= 1024.0
        return f"{value:.1f}T"


def _status_kind_for_state(state: str) -> StatusKind:
    text = (state or "").strip().lower()
    if not text:
        return StatusKind.IDLE
    if any(token in text for token in ("fail", "offl", "missing", "error", "bad", "dead")):
        return StatusKind.BAD
    if any(
        token in text
        for token in ("dgrd", "degrad", "rebuild", "init", "foreign", "frgn", "warn", "rbld")
    ):
        return StatusKind.WARN
    if any(token in text for token in ("optl", "online", "ok", "ready", "normal", "ugood", "onln")):
        return StatusKind.OK
    return StatusKind.IDLE


_ICON_FOR_KIND = {
    StatusKind.OK: ICON_OK,
    StatusKind.WARN: ICON_WARNING,
    StatusKind.BAD: ICON_BAD,
    StatusKind.IDLE: ICON_IDLE,
    StatusKind.RUNNING: ICON_RUNNING,
    StatusKind.SELECTED: ICON_SELECTED,
}


@dataclass
class VirtualDiskView:
    """A flattened ``VolumeInfo`` with controller context."""

    volume_id: str
    controller_index: int
    vendor: str
    level: str
    level_label: str
    name: str
    state: str
    size_bytes: int
    size_human: str
    disk_ids: List[str]
    spans: List[List[str]]
    members_label: str
    status_kind: StatusKind = StatusKind.IDLE
    raw: Optional[VolumeInfo] = None

    @property
    def status_icon(self) -> str:
        return _ICON_FOR_KIND.get(self.status_kind, ICON_IDLE)

    @property
    def key(self) -> str:
        return f"c{self.controller_index}:{self.volume_id}"

    @classmethod
    def from_volume(cls, controller_index: int, vendor: str, volume: VolumeInfo) -> "VirtualDiskView":
        state = volume.state or ""
        return cls(
            volume_id=volume.volume_id,
            controller_index=controller_index,
            vendor=vendor,
            level=volume.level or volume.raw_level or "-",
            level_label=volume.level_label,
            name=volume.name or "-",
            state=state or "-",
            size_bytes=volume.size_bytes,
            size_human=volume.size_human,
            disk_ids=list(volume.disk_ids),
            spans=[list(span) for span in volume.spans],
            members_label=volume.members_label,
            status_kind=_status_kind_for_state(state),
            raw=volume,
        )


@dataclass
class TaskItem:
    """One queued/running/completed UI task."""

    id: str
    kind: TaskKind
    title: str
    status: TaskStatus = TaskStatus.QUEUED
    created_at: str = field(default_factory=utc_now_iso)
    started_at: str = ""
    finished_at: str = ""
    controller_index: Optional[int] = None
    disk_ids: List[str] = field(default_factory=list)
    volume_id: str = ""
    done: int = 0
    total: int = 0
    log_lines: Deque[str] = field(default_factory=lambda: deque(maxlen=500))
    error: str = ""
    epoch: int = 0
    report_path: str = ""

    @property
    def progress_text(self) -> str:
        if self.total:
            return f"{self.done}/{self.total}"
        return "-"

    @property
    def target(self) -> str:
        if self.volume_id:
            return self.volume_id
        if self.disk_ids:
            if len(self.disk_ids) == 1:
                return self.disk_ids[0]
            return f"{len(self.disk_ids)} disks"
        if self.controller_index is not None:
            return f"c{self.controller_index}"
        return "-"

    @property
    def status_icon(self) -> str:
        return self.status.icon

    def log(self, line: str) -> None:
        if line:
            self.log_lines.append(line)


@dataclass
class ConfirmRequest:
    """Everything ConfirmModal needs, with no dependency on a specific screen."""

    title: str
    action: str
    ack_word: str
    ack_label: str
    audit_token: str
    commands: List[str] = field(default_factory=list)
    summary: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    danger: bool = True
    epoch: int = 0
    snapshot_name: str = "confirm"
    event_prefix: str = "confirm"


__all__ = [
    "BreadcrumbItem",
    "ConfirmRequest",
    "EvidenceFile",
    "EvidenceSessionView",
    "FilterState",
    "HotkeyHint",
    "ICON_BAD",
    "ICON_IDLE",
    "ICON_OK",
    "ICON_RUNNING",
    "ICON_SELECTED",
    "ICON_WARNING",
    "LayoutMode",
    "PageId",
    "RefreshScope",
    "RefreshState",
    "ReportState",
    "ReportStatus",
    "StatusKind",
    "TaskItem",
    "TaskKind",
    "TaskStatus",
    "VirtualDiskView",
]
