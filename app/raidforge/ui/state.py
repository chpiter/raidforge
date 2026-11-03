"""Shared mutable state handed from the shell entry point down to the screens.

Keeping one context object avoids global variables and makes every screen
individually testable: construct an :class:`AppContext` with fake members and
the UI renders without touching hardware.
"""

from __future__ import annotations

import contextlib
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from ..config import Config
from ..core.evidence import EvidenceSession
from ..core.models import (
    DetectionResult,
    DiskInfo,
    EraseRun,
    RaidActionRun,
    RaidBundle,
    RaidCapabilities,
    RaidTopology,
    SnapshotRecord,
    SubmitResult,
)
from ..core.publish import PublishState
from ..core.serverid import ServerId


@dataclass
class AppContext:
    """Everything the screens need to render or act."""

    cfg: Config
    evidence: EvidenceSession
    server_id: ServerId
    detection: Optional[DetectionResult] = None
    disks: List[DiskInfo] = field(default_factory=list)
    #: Latest RAID topology/capability bundle for every detected controller.
    raid: Optional[RaidBundle] = None
    raid_history: List[RaidActionRun] = field(default_factory=list)
    #: Bumped when a RAID action is confirmed; tags its screenshots and record.
    raid_epoch: int = 0
    active_controller: Optional[int] = None
    #: Cumulative inventory for the whole boot.  ``disks`` is only the latest
    #: scan (what the TUI shows); the DOCX report uses this stable snapshot.
    inventory: List[DiskInfo] = field(default_factory=list)
    selection: List[str] = field(default_factory=list)
    results: List[SubmitResult] = field(default_factory=list)
    #: Every completed erase run in this boot. ``results`` only holds the most
    #: recent run; the DOCX report is built from this cumulative list.
    erase_history: List[EraseRun] = field(default_factory=list)
    #: Screenshots written so far, tagged with the run epoch that produced them.
    snapshot_log: List[SnapshotRecord] = field(default_factory=list)
    #: Bumped when a confirmation modal opens; tags its screenshot and the run.
    run_epoch: int = 0
    network_ready: bool = False
    #: ``"eno1 192.168.40.50"`` once a lease is seen (empty while waiting).
    network_detail: str = ""
    last_message: str = ""

    #: Set when the operator asks to stop the current erase run.
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)
    #: Set when the operator asks to leave the application entirely.
    shutdown_event: threading.Event = field(default_factory=threading.Event, repr=False)
    #: Serialises report/manifest/upload publishes (run and exit paths).
    publish_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    #: Serialises RAID topology collection and RAID execution.
    raid_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    #: Latest publish status for the Progress/Dashboard screens.
    publish_state: PublishState = field(default_factory=PublishState, repr=False)
    _busy: int = field(default=0, repr=False)
    _busy_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # -- background work ----------------------------------------------------
    def spawn(self, fn: Callable[[], None], *, name: str = "erase-worker") -> threading.Thread:
        """Run ``fn`` on a *daemon* thread and keep the exit path honest.

        Textual's ``run_worker(thread=True)`` uses the loop's default executor.
        Threads cannot be cancelled, and ``asyncio.run()`` shuts that executor
        down when the app exits - so a worker that is still sleeping (the 30s
        route wait, a 60s vendor command) blocks ``App.run()`` from returning.
        That is exactly how "press Q" turned into a frozen console.

        A daemon thread sidesteps the whole mechanism: it never holds the
        interpreter open, and the process can exit while it is still running.
        """

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
        """How many background jobs are still running."""

        with self._busy_lock:
            return self._busy

    def wait_idle(self, timeout: float = 5.0) -> bool:
        """Wait (bounded) for background jobs to finish.

        Used before the evidence manifest is hashed: a worker that appends to
        ``events.jsonl`` afterwards would invalidate the manifest SHA-256.
        """

        deadline = time.monotonic() + max(0.0, timeout)
        while time.monotonic() < deadline:
            if self.busy == 0:
                return True
            time.sleep(0.05)
        return self.busy == 0

    @property
    def selected_disks(self) -> List[DiskInfo]:
        """Currently selected disks, resolved against the full inventory."""

        by_id: Dict[str, DiskInfo] = {disk.disk_id: disk for disk in self.disks}
        return [by_id[key] for key in self.selection if key in by_id]


    def disk_by_id(self, disk_id: str) -> Optional[DiskInfo]:
        """Look up one disk by its identifier."""

        for disk in self.disks:
            if disk.disk_id == disk_id:
                return disk
        return None

    def merge_inventory(self, disks: List[DiskInfo]) -> None:
        """Merge a scan result into the boot-long inventory (ordered by id)."""

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
        self.disks = list(getattr(scan, "disks", []) or [])
        self.merge_inventory(self.disks)
        self.raid = getattr(scan, "raid", None)
        self.selection = []
        if self.active_controller is not None and self.raid is not None:
            if self.raid.topology(self.active_controller) is None:
                self.active_controller = None

    def raid_topology(self, controller_index: int) -> Optional[RaidTopology]:
        """Return the latest topology for one controller."""

        if self.raid is None:
            return None
        return self.raid.topology(controller_index)

    def raid_capabilities(self, controller_index: int) -> RaidCapabilities:
        """Return the latest capability set for one controller."""

        if self.raid is None:
            return RaidCapabilities()
        return self.raid.capabilities_for(controller_index)

    def controller_disks(self, controller_index: int) -> List[DiskInfo]:
        """Physical disks that belong to one controller."""

        return [disk for disk in self.disks if disk.controller_index == controller_index]

    def controller_by_index(self, controller_index: int):
        """Return the detection record for one controller, if present."""

        if self.detection is None:
            return None
        for controller in self.detection.controllers:
            if controller.index == controller_index:
                return controller
        return None

    def counts(self) -> Dict[str, int]:
        """Return per-media counts used by the dashboard summary."""

        summary = {"HDD": 0, "SSD": 0, "UNKNOWN": 0, "SELECTED": 0}
        for disk in self.disks:
            summary[disk.medium.value] = summary.get(disk.medium.value, 0) + 1
        summary["SELECTED"] = len(self.selection)
        return summary
