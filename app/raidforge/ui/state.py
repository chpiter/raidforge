"""Single mutable state object shared by the seven modules.

The runtime creates exactly one :class:`AppState` in
:func:`raidforge.main.build_context` and registers it with
:meth:`AppState.activate`.  Tests may construct independent instances; screens
always receive the instance explicitly, so the singleton never becomes a hidden
dependency.

The required domain fields are deliberately materialised:
``controllers``, ``physical_disks``, ``virtual_disks`` and ``task_queue``.
``disks`` and ``selection`` remain as compatibility properties because the
existing core-facing code and tests use those names.
"""

from __future__ import annotations

import getpass
import threading
import time
from dataclasses import dataclass, field
from typing import ClassVar, Dict, List, Optional, Set, Tuple

from ..config import Config
from ..core.evidence import EvidenceSession
from ..core.models import (
    ControllerInfo,
    DetectionResult,
    DiskInfo,
    EraseRun,
    RaidActionRun,
    RaidBundle,
    RaidCapabilities,
    RaidTopology,
    SnapshotRecord,
    SubmitResult,
    utc_now_iso,
)
from ..core.publish import PublishState
from ..core.serverid import ServerId
from .models import (
    BreadcrumbItem,
    EvidenceSessionView,
    FilterState,
    HotkeyHint,
    LayoutMode,
    PageId,
    RefreshScope,
    RefreshState,
    ReportState,
    ReportStatus,
    StatusKind,
    TaskItem,
    VirtualDiskView,
)


def _login_user() -> str:
    try:
        return getpass.getuser()
    except Exception:  # pragma: no cover - containers without passwd entries
        return "root"


@dataclass
class AppState:
    """Everything the UI needs to render or act."""

    # -- construction -------------------------------------------------------
    cfg: Config
    evidence: EvidenceSession
    server_id: ServerId

    # -- detection / inventory (existing semantics) -------------------------
    detection: Optional[DetectionResult] = None
    physical_disks: List[DiskInfo] = field(default_factory=list)
    #: Latest RAID topology/capability bundle for every detected controller.
    raid: Optional[RaidBundle] = None
    raid_history: List[RaidActionRun] = field(default_factory=list)
    #: Bumped when a RAID action is confirmed; tags its screenshots and record.
    raid_epoch: int = 0
    active_controller: Optional[int] = None
    #: Cumulative inventory for the whole boot.  ``physical_disks`` is only the
    #: latest scan (what the TUI shows); the DOCX report uses this stable list.
    inventory: List[DiskInfo] = field(default_factory=list)
    results: List[SubmitResult] = field(default_factory=list)
    #: Every completed erase run in this boot.
    erase_history: List[EraseRun] = field(default_factory=list)
    #: Screenshots written so far, tagged with the run epoch that produced them.
    snapshot_log: List[SnapshotRecord] = field(default_factory=list)
    #: Bumped when a confirmation modal opens; tags its screenshot and the run.
    run_epoch: int = 0
    network_ready: bool = False
    #: ``"eno1 192.168.40.50"`` once a lease is seen (empty while waiting).
    network_detail: str = ""
    ip_address: str = ""
    login_user: str = field(default_factory=_login_user)
    last_message: str = ""

    # -- required domain state ---------------------------------------------
    controllers: List[ControllerInfo] = field(default_factory=list)
    virtual_disks: List[VirtualDiskView] = field(default_factory=list)
    task_queue: List[TaskItem] = field(default_factory=list)

    # -- navigation / page state -------------------------------------------
    active_page: PageId = PageId.DASHBOARD
    page_history: List[PageId] = field(default_factory=list)
    layout_mode: LayoutMode = LayoutMode.STANDARD
    active_volume_id: Optional[str] = None
    physical_selection: Set[str] = field(default_factory=set)
    physical_cursor: Optional[str] = None
    virtual_cursor: Optional[str] = None
    controller_cursor: Optional[int] = None
    task_cursor: Optional[str] = None
    evidence_cursor: Optional[str] = None
    filters: Dict[str, FilterState] = field(
        default_factory=lambda: {
            "physical": FilterState(),
            "virtual": FilterState(),
        }
    )
    scroll_positions: Dict[str, Tuple[int, int]] = field(default_factory=dict)
    breadcrumb_base: List[BreadcrumbItem] = field(default_factory=list)
    breadcrumb_stack: List[BreadcrumbItem] = field(default_factory=list)
    hotkeys: List[HotkeyHint] = field(default_factory=list)

    # -- refresh / tasks / evidence ----------------------------------------
    refresh: RefreshState = field(default_factory=RefreshState)
    active_task_id: Optional[str] = None
    report_status: ReportStatus = field(default_factory=ReportStatus)
    evidence_sessions: List[EvidenceSessionView] = field(default_factory=list)
    task_runner: object = field(default=None, repr=False)
    services: object = field(default=None, repr=False)

    # -- concurrency (existing semantics) ----------------------------------
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)
    shutdown_event: threading.Event = field(default_factory=threading.Event, repr=False)
    publish_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    raid_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    publish_state: PublishState = field(default_factory=PublishState, repr=False)
    _busy: int = field(default=0, repr=False)
    _busy_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    #: Runtime singleton; screens still receive the instance explicitly.
    _instance: ClassVar[Optional["AppState"]] = None

    # -- singleton ----------------------------------------------------------
    @classmethod
    def activate(cls, state: "AppState") -> "AppState":
        cls._instance = state
        return state

    @classmethod
    def current(cls) -> Optional["AppState"]:
        return cls._instance

    # -- compatibility aliases ---------------------------------------------
    @property
    def server_sn(self) -> str:
        return self.server_id.serial

    @property
    def disks(self) -> List[DiskInfo]:
        return self.physical_disks

    @disks.setter
    def disks(self, value: List[DiskInfo]) -> None:
        self.physical_disks = list(value)

    @property
    def selection(self) -> List[str]:
        return sorted(self.physical_selection)

    @selection.setter
    def selection(self, value: List[str]) -> None:
        self.physical_selection = set(value or [])

    # -- background work (unchanged from the old AppContext) ---------------
    def spawn(self, fn, *, name: str = "erase-worker") -> threading.Thread:
        """Run ``fn`` on a daemon thread and keep the exit path honest."""

        with self._busy_lock:
            self._busy += 1

        def _run() -> None:
            try:
                fn()
            except Exception:  # pragma: no cover - workers must not crash the app
                pass
            finally:
                with self._busy_lock:
                    self._busy -= 1

        thread = threading.Thread(target=_run, name=name, daemon=True)
        thread.start()
        return thread

    @property
    def busy(self) -> int:
        with self._busy_lock:
            return self._busy

    def wait_idle(self, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        while time.monotonic() < deadline:
            if self.busy == 0:
                return True
            time.sleep(0.05)
        return self.busy == 0

    # -- inventory / topology ----------------------------------------------
    @property
    def selected_disks(self) -> List[DiskInfo]:
        by_id: Dict[str, DiskInfo] = {disk.disk_id: disk for disk in self.physical_disks}
        return [by_id[key] for key in self.selection if key in by_id]

    def disk_by_id(self, disk_id: str) -> Optional[DiskInfo]:
        for disk in self.physical_disks:
            if disk.disk_id == disk_id:
                return disk
        return None

    def merge_inventory(self, disks: List[DiskInfo]) -> None:
        by_id = {disk.disk_id: index for index, disk in enumerate(self.inventory)}
        for disk in disks:
            index = by_id.get(disk.disk_id)
            if index is None:
                by_id[disk.disk_id] = len(self.inventory)
                self.inventory.append(disk)
            else:
                self.inventory[index] = disk

    def apply_scan(self, scan: object) -> None:
        """Apply a :class:`~raidforge.core.scan.ScanResult` to the UI state."""

        self.detection = getattr(scan, "detection", None)
        self.physical_disks = list(getattr(scan, "disks", []) or [])
        self.merge_inventory(self.physical_disks)
        self.raid = getattr(scan, "raid", None)
        self.controllers = list(getattr(self.detection, "controllers", []) or [])
        self.refresh_virtual_views()
        self.physical_selection.clear()
        self.prune_selection()
        if self.active_controller is not None and self.raid is not None:
            if self.raid.topology(self.active_controller) is None:
                self.active_controller = None

    def replace_controller_disks(self, controller_index: int, disks: List[DiskInfo]) -> None:
        """Replace only one controller's disks, leaving the rest untouched."""

        kept = [disk for disk in self.physical_disks if disk.controller_index != controller_index]
        replacement = [disk for disk in disks if disk.controller_index == controller_index]
        self.physical_disks = sorted(
            kept + replacement, key=lambda disk: (disk.controller_index, disk.disk_id)
        )
        self.merge_inventory(replacement)
        self.prune_selection()

    def replace_topology(
        self, topology: RaidTopology, capabilities: Optional[RaidCapabilities] = None
    ) -> None:
        """Replace one controller's topology/capability without rescanning the world."""

        if self.raid is None:
            return
        index = topology.controller_index
        for position, existing in enumerate(self.raid.topologies):
            if existing.controller_index == index:
                self.raid.topologies[position] = topology
                break
        else:
            self.raid.topologies.append(topology)
        if capabilities is not None:
            self.raid.capabilities[index] = capabilities
        self.refresh_virtual_views()

    def refresh_virtual_views(self) -> None:
        views: List[VirtualDiskView] = []
        if self.raid is not None:
            for topology in self.raid.topologies:
                vendor = (
                    topology.vendor.value
                    if hasattr(topology.vendor, "value")
                    else str(topology.vendor)
                )
                for volume in topology.volumes:
                    views.append(
                        VirtualDiskView.from_volume(topology.controller_index, vendor, volume)
                    )
        views.sort(key=lambda view: (view.controller_index, view.volume_id))
        self.virtual_disks = views
        if self.active_volume_id and self.volume_by_id(self.active_volume_id) is None:
            self.active_volume_id = None

    def raid_topology(self, controller_index: int) -> Optional[RaidTopology]:
        if self.raid is None:
            return None
        return self.raid.topology(controller_index)

    def raid_capabilities(self, controller_index: int) -> RaidCapabilities:
        if self.raid is None:
            return RaidCapabilities()
        return self.raid.capabilities_for(controller_index)

    def controller_disks(self, controller_index: int) -> List[DiskInfo]:
        return [disk for disk in self.physical_disks if disk.controller_index == controller_index]

    def controller_by_index(self, controller_index: int) -> Optional[ControllerInfo]:
        for controller in self.controllers:
            if controller.index == controller_index:
                return controller
        if self.detection is not None:
            for controller in self.detection.controllers:
                if controller.index == controller_index:
                    return controller
        return None

    def counts(self) -> Dict[str, int]:
        summary = {"HDD": 0, "SSD": 0, "UNKNOWN": 0, "SELECTED": 0}
        for disk in self.physical_disks:
            summary[disk.medium.value] = summary.get(disk.medium.value, 0) + 1
        summary["SELECTED"] = len(self.physical_selection)
        return summary

    # -- UI helpers ---------------------------------------------------------
    def filter_for(self, page: str) -> FilterState:
        return self.filters.setdefault(page, FilterState())

    def volume_by_id(
        self, volume_id: str, controller_index: Optional[int] = None
    ) -> Optional[VirtualDiskView]:
        for view in self.virtual_disks:
            if view.volume_id != volume_id:
                continue
            if controller_index is not None and view.controller_index != controller_index:
                continue
            return view
        return None

    def members_for_volume(self, view: VirtualDiskView) -> List[DiskInfo]:
        by_id = {disk.disk_id: disk for disk in self.physical_disks}
        return [by_id[disk_id] for disk_id in view.disk_ids if disk_id in by_id]

    def disk_membership(self) -> Dict[str, str]:
        membership: Dict[str, str] = {}
        for view in self.virtual_disks:
            for disk_id in view.disk_ids:
                membership[disk_id] = view.volume_id
        return membership

    def blocked_reason(self, disk: DiskInfo) -> str:
        if not disk.selectable:
            return "media type UNKNOWN - selection forbidden"
        membership = self.disk_membership()
        if disk.disk_id in membership:
            return f"member of {membership[disk.disk_id]}"
        topology = self.raid_topology(disk.controller_index)
        if topology is not None and disk.disk_id in topology.blocked:
            return topology.blocked[disk.disk_id]
        return ""

    def prune_selection(self) -> None:
        valid = {disk.disk_id for disk in self.physical_disks if disk.selectable}
        valid -= set(self.disk_membership())
        self.physical_selection &= valid
        if self.physical_cursor and self.disk_by_id(self.physical_cursor) is None:
            self.physical_cursor = None

    @property
    def recent_tasks(self) -> List[TaskItem]:
        return list(reversed(self.task_queue[-5:]))

    def storage_summary(self) -> Dict[str, object]:
        by_media = {"HDD": 0, "SSD": 0, "UNKNOWN": 0}
        total_bytes = 0
        for disk in self.physical_disks:
            by_media[disk.medium.value] = by_media.get(disk.medium.value, 0) + 1
            total_bytes += int(disk.size_bytes or 0)
        members = {disk_id for view in self.virtual_disks for disk_id in view.disk_ids}
        return {
            "disks": len(self.physical_disks),
            "volumes": len(self.virtual_disks),
            "by_media": by_media,
            "total_bytes": total_bytes,
            "free": max(0, len(self.physical_disks) - len(members)),
        }

    def health_summary(self) -> Tuple[StatusKind, List[str]]:
        """Return the worst health kind and the reasons behind it."""

        issues: List[str] = []
        bad = False
        warn = False
        if self.refresh.error:
            issues.append(f"refresh failed: {self.refresh.error}")
            bad = True
        for controller in self.controllers:
            topology = self.raid_topology(controller.index)
            if topology is None:
                issues.append(f"c{controller.index}: topology not collected")
                warn = True
            elif topology.error:
                issues.append(f"c{controller.index}: {topology.error}")
                bad = True
        if any(not disk.selectable for disk in self.physical_disks):
            warn = True
        if any(task.status.value == "failed" for task in self.task_queue):
            issues.append("one or more tasks failed")
            bad = True
        if any(task.status.value in ("partial", "cancelled") for task in self.task_queue):
            issues.append("one or more tasks need review")
            warn = True
        if self.report_status.state is ReportState.FAILED:
            issues.append("report/upload failed")
            bad = True
        if self.report_status.queued:
            issues.append("evidence queued locally")
            warn = True
        if bad:
            return StatusKind.BAD, issues
        if warn:
            return StatusKind.WARN, issues
        return StatusKind.OK, issues or ["all checks passed"]

    def mark_refresh_started(self, scope: RefreshScope) -> None:
        self.refresh.active = True
        self.refresh.scope = scope
        self.refresh.started_at = utc_now_iso()
        self.refresh.error = ""
        self.refresh.generation += 1

    def mark_refresh_finished(self, error: str = "") -> None:
        self.refresh.active = False
        self.refresh.finished_at = utc_now_iso()
        self.refresh.error = error
        self.refresh.generation += 1

    def set_report_status(self, status: ReportStatus) -> None:
        self.report_status = status

    def breadcrumb_items(self) -> List[BreadcrumbItem]:
        return list(self.breadcrumb_base) + list(self.breadcrumb_stack)

    def push_breadcrumb(self, label: str, page: Optional[PageId] = None) -> None:
        self.breadcrumb_stack.append(BreadcrumbItem(label, page))

    def pop_breadcrumb(self) -> None:
        if self.breadcrumb_stack:
            self.breadcrumb_stack.pop()

    def set_page(self, page: PageId) -> None:
        self.active_page = page
        self.breadcrumb_base = [BreadcrumbItem(page.title, page)]
        self.breadcrumb_stack.clear()

    # -- confirmation -------------------------------------------------------
    def ack_word(self, action: str, controller_index: Optional[int] = None) -> str:
        """Every destructive action uses the short, deliberate ``yes``."""

        return "yes"

    def audit_token(self) -> str:
        """Audit metadata only; never used as the typed acknowledgement."""

        return self.server_id.confirm_token


#: Backwards-compatible name used by the existing core-facing modules/tests.
AppContext = AppState


__all__ = ["AppContext", "AppState"]
