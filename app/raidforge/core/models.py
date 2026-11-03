"""Core data models for the erase application.

Every payload that crosses a module boundary (detection -> enumeration ->
command construction -> execution -> evidence -> UI) is modelled here as a plain
dataclass.  The UI layer must never see the raw JSON produced by ``storcli``,
``arcconf`` or ``ssacli``.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple


def utc_now_iso() -> str:
    """Return the current UTC instant as a second-precision ISO-8601 string."""

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Vendor(str, Enum):
    """Storage controller vendor families we know how to talk to."""

    LSI = "lsi"
    PMC = "pmc"
    HPE = "hpe"
    GENERIC = "generic"

    @property
    def label(self) -> str:
        """Human readable vendor label shown in the UI."""

        return {
            Vendor.LSI: "LSI / Broadcom RAID (storcli/perccli)",
            Vendor.PMC: "PMC / Adaptec / Microchip RAID (arcconf)",
            Vendor.HPE: "HPE Smart Array (ssacli/hpssacli)",
            Vendor.GENERIC: "Generic SAS/SATA/NVMe (sg3_utils + smartctl)",
        }[self]


class Medium(str, Enum):
    """Physical recording medium of a disk."""

    HDD = "HDD"
    SSD = "SSD"
    UNKNOWN = "UNKNOWN"


class Protocol(str, Enum):
    """Transport protocol reported by the controller."""

    SAS = "SAS"
    SATA = "SATA"
    NVME = "NVMe"
    UNKNOWN = "UNKNOWN"


class SubmitStatus(str, Enum):
    """Outcome of *submitting* an erase command.

    Note there is deliberately no ``COMPLETED`` value: by contract the TUI never
    waits for a vendor erase to finish (see README, section "Completion
    semantics").  A return code of zero only means the command was accepted by
    the controller and is running asynchronously on the firmware side.
    """

    SUBMITTED = "SUBMITTED"
    ASYNC_SUBMITTED = "ASYNC_SUBMITTED"
    FAILED = "FAILED"
    TIMEOUT = "TIMEOUT"
    SKIPPED = "SKIPPED"
    CANCELLED = "CANCELLED"


@dataclass
class ControllerInfo:
    """One RAID controller (or the synthetic generic controller)."""

    index: int
    vendor: Vendor
    model: str = ""
    firmware: str = ""
    serial: str = ""
    #: Canonical tool name that produced this controller (storcli/perccli/...).
    tool: str = ""
    #: Resolved binary name (``storcli64``/``hpssacli``/...), empty when unknown.
    binary: str = ""
    #: Vendor-neutral key/value details parsed from the controller query.
    details: Dict[str, str] = field(default_factory=dict)
    raw_json: Dict[str, Any] = field(default_factory=dict)

    @property
    def controller_id(self) -> str:
        """Short identifier used in command templates, e.g. ``c0``."""

        return f"c{self.index}"

    def to_json_obj(self) -> Dict[str, Any]:
        """Serialise to a JSON-safe dict."""

        obj = asdict(self)
        obj["vendor"] = self.vendor.value
        obj["controller_id"] = self.controller_id
        return obj


@dataclass
class DiskInfo:
    """One physical disk, normalised across all four tool families."""

    disk_id: str
    controller_index: int
    vendor: Vendor
    medium: Medium = Medium.UNKNOWN
    protocol: Protocol = Protocol.UNKNOWN
    enclosure: Optional[int] = None
    slot: Optional[int] = None
    channel: Optional[int] = None
    device_number: Optional[int] = None
    model: str = ""
    serial: str = ""
    firmware: str = ""
    wwn: str = ""
    size_bytes: int = 0
    raid_state: str = ""
    device_node: str = ""
    scsi_generic_node: str = ""
    controller_ref: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def selectable(self) -> bool:
        """Constraint 8: ``UNKNOWN`` media can never be erased / selected."""

        return self.medium is not Medium.UNKNOWN

    @property
    def size_human(self) -> str:
        """Human readable capacity."""

        return format_bytes(self.size_bytes)

    @property
    def location(self) -> str:
        """Short human location string shown in the disk table."""

        if self.device_node:
            return self.device_node
        if self.enclosure is not None and self.slot is not None:
            return f"e{self.enclosure}/s{self.slot}"
        if self.channel is not None and self.device_number is not None:
            return f"ch{self.channel}/dev{self.device_number}"
        return self.controller_ref or "-"

    @property
    def firmware_or_raid(self) -> str:
        """One unambiguous label for the report/TUI firmware-or-RAID column.

        Generic/NVMe disks only have a firmware revision; controller-managed
        disks expose a RAID state (and sometimes firmware too).  Prefixing the
        value prevents a firmware string from ever being read as RAID state.
        """

        raid = str(self.raid_state or "").strip()
        firmware = str(self.firmware or "").strip()
        if raid and firmware:
            return f"RAID {raid} / FW {firmware}"
        if raid:
            return f"RAID {raid}"
        if firmware:
            return f"FW {firmware}"
        return "-"

    def to_json_obj(self) -> Dict[str, Any]:
        """Serialise to a JSON-safe dict (also used by the evidence manifest)."""

        obj = asdict(self)
        obj["vendor"] = self.vendor.value
        obj["medium"] = self.medium.value
        obj["protocol"] = self.protocol.value
        obj["selectable"] = self.selectable
        obj["size_human"] = self.size_human
        return obj


@dataclass
class CommandPlan:
    """A fully constructed, not-yet-executed erase command."""

    disk_id: str
    kind: str
    label: str
    argv: List[str]
    medium: Medium
    vendor: Vendor
    notes: str = ""

    @property
    def dry_run_text(self) -> str:
        """Shell-quoted command line shown by :class:`ConfirmModal` (dry run)."""

        return " ".join(shlex_quote(part) for part in self.argv)

    def to_json_obj(self) -> Dict[str, Any]:
        """Serialise to a JSON-safe dict."""

        obj = asdict(self)
        obj["medium"] = self.medium.value
        obj["vendor"] = self.vendor.value
        obj["argv"] = list(self.argv)
        obj["dry_run_text"] = self.dry_run_text
        return obj


@dataclass
class SubmitResult:
    """Result of running one :class:`CommandPlan`."""

    disk_id: str
    kind: str
    status: SubmitStatus
    return_code: Optional[int]
    duration_sec: float
    stdout: str = ""
    stderr: str = ""
    argv: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True when the command was accepted by the controller."""

        return self.status in (SubmitStatus.SUBMITTED, SubmitStatus.ASYNC_SUBMITTED)

    def to_json_obj(self) -> Dict[str, Any]:
        """Serialise to a JSON-safe dict."""

        obj = asdict(self)
        obj["status"] = self.status.value
        obj["argv"] = list(self.argv)
        return obj


@dataclass
class SnapshotRecord:
    """One screenshot on disk, tagged with the erase run that produced it.

    The progress screen schedules its snapshot asynchronously, so the file can
    land *after* the run is recorded.  The report therefore selects images at
    finalisation time by ``run_epoch`` instead of storing paths on the run.
    """

    name: str
    stem: str
    path: str
    run_epoch: int = 0
    captured_at: str = field(default_factory=utc_now_iso)

    def to_json_obj(self) -> Dict[str, Any]:
        """Serialise for the evidence log."""

        return asdict(self)


@dataclass
class EraseRun:
    """Cumulative record of one erase run inside a single boot session.

    ``ctx.results`` is replaced after every run, so this object is the only
    in-memory history the report can rely on.  ``execute_plans`` returns a
    prefix of ``plans`` in the same order; a missing result means the operator
    cancelled or the run aborted before that command was submitted.
    """

    index: int
    run_epoch: int
    started_at: str
    finished_at: str
    dry_run: bool
    cancelled: bool
    plans: List[CommandPlan] = field(default_factory=list)
    results: List[SubmitResult] = field(default_factory=list)
    disks: Dict[str, DiskInfo] = field(default_factory=dict)

    def _results_by_disk(self) -> Dict[str, SubmitResult]:
        """First result per disk id (execute_plans submits each plan once)."""

        by_id: Dict[str, SubmitResult] = {}
        for result in self.results:
            by_id.setdefault(result.disk_id, result)
        return by_id

    def attempts(self) -> List[Tuple[CommandPlan, Optional[SubmitResult]]]:
        """Pair every plan with the result carrying the same ``disk_id``."""

        by_id = self._results_by_disk()
        return [(plan, by_id.get(plan.disk_id)) for plan in self.plans]

    def unmatched_results(self) -> List[SubmitResult]:
        """Results whose ``disk_id`` has no matching plan (never silently drop)."""

        plan_ids = {plan.disk_id for plan in self.plans}
        return [result for result in self.results if result.disk_id not in plan_ids]

    def status_counts(self) -> Dict[str, int]:
        """Per-run totals used by the report summary."""

        counts = {
            "planned": len(self.plans),
            "submitted": 0,
            "failed": 0,
            "timeout": 0,
            "skipped": 0,
            "cancelled": 0,
            "not_attempted": 0,
            "unmatched": len(self.unmatched_results()),
        }
        for _plan, result in self.attempts():
            if result is None:
                key = "cancelled" if self.cancelled else "not_attempted"
                counts[key] += 1
                continue
            if result.ok:
                counts["submitted"] += 1
            elif result.status is SubmitStatus.FAILED:
                counts["failed"] += 1
            elif result.status is SubmitStatus.TIMEOUT:
                counts["timeout"] += 1
            elif result.status is SubmitStatus.SKIPPED:
                counts["skipped"] += 1
            elif result.status is SubmitStatus.CANCELLED:
                counts["cancelled"] += 1
            else:  # pragma: no cover - defensive
                counts["failed"] += 1
        return counts

    def to_json_obj(self) -> Dict[str, Any]:
        """Compact summary for ``manifest.json`` (no raw vendor payloads)."""

        attempts: List[Dict[str, Any]] = []
        for plan, result in self.attempts():
            if result is None:
                status = "CANCELLED_UNSUBMITTED" if self.cancelled else "NOT_ATTEMPTED"
                return_code = None
                duration = None
            else:
                status = result.status.value
                return_code = result.return_code
                duration = round(float(result.duration_sec), 3)
            attempts.append(
                {
                    "disk_id": plan.disk_id,
                    "kind": plan.kind,
                    "label": plan.label,
                    "planned": True,
                    "status": status,
                    "return_code": return_code,
                    "duration_sec": duration,
                }
            )
        for result in self.unmatched_results():
            attempts.append(
                {
                    "disk_id": result.disk_id,
                    "kind": result.kind,
                    "label": result.kind,
                    "planned": False,
                    "status": result.status.value,
                    "return_code": result.return_code,
                    "duration_sec": round(float(result.duration_sec), 3),
                }
            )
        return {
            "index": self.index,
            "run_epoch": self.run_epoch,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "dry_run": self.dry_run,
            "cancelled": self.cancelled,
            "counts": self.status_counts(),
            "disks": [
                {
                    "disk_id": disk.disk_id,
                    "serial": disk.serial,
                    "firmware": disk.firmware,
                    "raid_state": disk.raid_state,
                    "size_bytes": disk.size_bytes,
                    "size_human": disk.size_human,
                    "medium": disk.medium.value,
                    "protocol": disk.protocol.value,
                    "model": disk.model,
                    "device_node": disk.device_node,
                    "location": disk.location,
                }
                for disk in self.disks.values()
            ],
            "attempts": attempts,
        }


@dataclass
class DetectionAttempt:
    """Audit record for one rung of the tool detection ladder."""

    tool: str
    vendor: str
    ok: bool
    return_code: Optional[int] = None
    binary: str = ""
    stderr: str = ""
    detail: str = ""

    def to_json_obj(self) -> Dict[str, Any]:
        """Serialise to a JSON-safe dict."""

        return asdict(self)


@dataclass
class DetectionResult:
    """Which tool won the ladder, plus everything used to reach that verdict."""

    tool: str
    vendor: Vendor
    controllers: List[ControllerInfo]
    binary: str = ""
    provider: str = ""
    attempts: List[DetectionAttempt] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True when at least one controller was discovered."""

        return bool(self.controllers)

    def to_json_obj(self) -> Dict[str, Any]:
        """Serialise to a JSON-safe dict."""

        return {
            "tool": self.tool,
            "vendor": self.vendor.value,
            "binary": self.binary,
            "provider": self.provider,
            "ok": self.ok,
            "controllers": [c.to_json_obj() for c in self.controllers],
            "attempts": [a.to_json_obj() for a in self.attempts],
        }


@dataclass
class SessionMeta:
    """Top level identity of an evidence session."""

    server_sn: str
    session_id: str
    root_dir: str
    started_at: str = field(default_factory=utc_now_iso)
    serial_source: str = ""
    tool: str = ""
    hostname: str = ""

    def to_json_obj(self) -> Dict[str, Any]:
        """Serialise to a JSON-safe dict."""

        return asdict(self)


class RaidLevel(str, Enum):
    """RAID levels this appliance can create."""

    RAID0 = "RAID0"
    RAID1 = "RAID1"
    RAID5 = "RAID5"
    RAID10 = "RAID10"

    @property
    def number(self) -> str:
        """Return the vendor-neutral level token (``0``/``1``/``5``/``10``)."""

        return self.value.replace("RAID", "")


class Raid0Mode(str, Enum):
    """How RAID0 is laid out over the selected disks."""

    SINGLE = "single"
    PER_DISK = "per-disk"


class RaidAction(str, Enum):
    """The three write operations the RAID layer supports."""

    CREATE = "create"
    DELETE_VD = "delete_vd"
    CLEAR_CONFIG = "clear_config"


class RaidStatus(str, Enum):
    """Outcome of submitting one RAID command."""

    ACCEPTED = "ACCEPTED"
    FAILED = "FAILED"
    TIMEOUT = "TIMEOUT"
    SKIPPED = "SKIPPED"
    CANCELLED = "CANCELLED"


@dataclass
class VolumeInfo:
    """One virtual drive / logical drive reported by a controller."""

    volume_id: str
    controller_index: int
    vendor: Vendor
    level: str = ""
    raw_level: str = ""
    name: str = ""
    state: str = ""
    size_bytes: int = 0
    disk_ids: List[str] = field(default_factory=list)
    spans: List[List[str]] = field(default_factory=list)
    disk_group: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def size_human(self) -> str:
        return format_bytes(self.size_bytes)

    @property
    def level_label(self) -> str:
        return self.level or self.raw_level or "-"

    @property
    def members_label(self) -> str:
        if not self.disk_ids:
            return "-"
        return ",".join(self.disk_ids)

    def to_json_obj(self) -> Dict[str, Any]:
        obj = asdict(self)
        obj["vendor"] = self.vendor.value
        obj["size_human"] = self.size_human
        obj["level_label"] = self.level_label
        return obj


@dataclass
class RaidLayout:
    """A validated (or being validated) RAID creation layout."""

    level: RaidLevel
    disks: List[DiskInfo]
    mode: Raid0Mode = Raid0Mode.SINGLE
    stripe_kb: int = 256
    name: str = ""
    spans: List[List[str]] = field(default_factory=list)
    volume_names: List[str] = field(default_factory=list)
    capacity_bytes: int = 0
    warnings: List[str] = field(default_factory=list)

    @property
    def capacity_human(self) -> str:
        return format_bytes(self.capacity_bytes)

    def to_json_obj(self) -> Dict[str, Any]:
        return {
            "level": self.level.value,
            "mode": self.mode.value,
            "stripe_kb": self.stripe_kb,
            "name": self.name,
            "spans": [list(span) for span in self.spans],
            "volume_names": list(self.volume_names),
            "capacity_bytes": self.capacity_bytes,
            "capacity_human": self.capacity_human,
            "warnings": list(self.warnings),
            "disks": [
                {
                    "disk_id": disk.disk_id,
                    "location": disk.location,
                    "serial": disk.serial,
                    "size_bytes": disk.size_bytes,
                    "size_human": disk.size_human,
                    "medium": disk.medium.value,
                    "protocol": disk.protocol.value,
                }
                for disk in self.disks
            ],
        }


@dataclass
class RaidCommandPlan:
    """A fully constructed RAID write command (never executed here)."""

    action: RaidAction
    controller_index: int
    vendor: Vendor
    label: str
    argv: List[str]
    level: str = ""
    disk_ids: List[str] = field(default_factory=list)
    volume_id: str = ""
    span_groups: List[List[str]] = field(default_factory=list)
    notes: str = ""

    @property
    def dry_run_text(self) -> str:
        return " ".join(shlex_quote(part) for part in self.argv)

    def to_json_obj(self) -> Dict[str, Any]:
        obj = asdict(self)
        obj["action"] = self.action.value
        obj["vendor"] = self.vendor.value
        obj["argv"] = list(self.argv)
        obj["dry_run_text"] = self.dry_run_text
        return obj


@dataclass
class RaidActionResult:
    """Result of submitting one RAID command."""

    action: RaidAction
    controller_index: int
    status: RaidStatus
    return_code: Optional[int]
    duration_sec: float
    argv: List[str] = field(default_factory=list)
    label: str = ""
    stdout: str = ""
    stderr: str = ""
    observed: str = "not_checked"
    observed_detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status is RaidStatus.ACCEPTED

    def to_json_obj(self) -> Dict[str, Any]:
        obj = asdict(self)
        obj["action"] = self.action.value
        obj["status"] = self.status.value
        obj["argv"] = list(self.argv)
        return obj


@dataclass
class RaidActionRun:
    """One operator-confirmed RAID action (one or more commands)."""

    index: int
    epoch: int
    action: RaidAction
    started_at: str
    finished_at: str
    dry_run: bool
    cancelled: bool
    controller_index: int
    plans: List[RaidCommandPlan] = field(default_factory=list)
    results: List[RaidActionResult] = field(default_factory=list)
    topology_before: Dict[str, Any] = field(default_factory=dict)
    topology_after: Dict[str, Any] = field(default_factory=dict)

    def status_counts(self) -> Dict[str, int]:
        counts = {
            "planned": len(self.plans),
            "accepted": 0,
            "failed": 0,
            "timeout": 0,
            "skipped": 0,
            "cancelled": 0,
            "not_attempted": 0,
        }
        for result in self.results:
            if result.status is RaidStatus.ACCEPTED:
                counts["accepted"] += 1
            elif result.status is RaidStatus.FAILED:
                counts["failed"] += 1
            elif result.status is RaidStatus.TIMEOUT:
                counts["timeout"] += 1
            elif result.status is RaidStatus.SKIPPED:
                counts["skipped"] += 1
            else:
                counts["cancelled"] += 1
        counts["not_attempted"] = max(0, counts["planned"] - len(self.results))
        return counts

    def to_json_obj(self) -> Dict[str, Any]:
        return {
            "index": self.index,
            "epoch": self.epoch,
            "action": self.action.value,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "dry_run": self.dry_run,
            "cancelled": self.cancelled,
            "controller_index": self.controller_index,
            "counts": self.status_counts(),
            "plans": [plan.to_json_obj() for plan in self.plans],
            "results": [result.to_json_obj() for result in self.results],
            "topology_before": self.topology_before,
            "topology_after": self.topology_after,
        }


@dataclass
class RaidCapabilities:
    """What a provider/controller combination can actually do."""

    levels: List[str] = field(default_factory=list)
    create: bool = False
    delete_vd: bool = False
    clear_config: bool = False
    raid0_per_disk: bool = False
    name_supported: bool = False
    stripe_supported: bool = False
    notes: List[str] = field(default_factory=list)

    def to_json_obj(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class RaidTopology:
    """Normalised RAID state for one controller."""

    controller_index: int
    vendor: Vendor
    tool: str
    binary: str
    volumes: List[VolumeInfo] = field(default_factory=list)
    membership: Dict[str, str] = field(default_factory=dict)
    free_disk_ids: List[str] = field(default_factory=list)
    blocked: Dict[str, str] = field(default_factory=dict)
    disk_states: Dict[str, str] = field(default_factory=dict)
    collected_at: str = field(default_factory=utc_now_iso)
    raw_files: List[str] = field(default_factory=list)
    error: str = ""

    def to_json_obj(self) -> Dict[str, Any]:
        return {
            "controller_index": self.controller_index,
            "vendor": self.vendor.value,
            "tool": self.tool,
            "binary": self.binary,
            "volumes": [volume.to_json_obj() for volume in self.volumes],
            "membership": dict(self.membership),
            "free_disk_ids": list(self.free_disk_ids),
            "blocked": dict(self.blocked),
            "disk_states": dict(self.disk_states),
            "collected_at": self.collected_at,
            "raw_files": list(self.raw_files),
            "error": self.error,
        }


@dataclass
class RaidBundle:
    """RAID topology + capabilities for every controller in this boot."""

    tool: str
    binary: str
    collected_at: str = field(default_factory=utc_now_iso)
    topologies: List[RaidTopology] = field(default_factory=list)
    capabilities: Dict[int, RaidCapabilities] = field(default_factory=dict)
    raw_outputs: List[Tuple[str, str]] = field(default_factory=list)
    error: str = ""

    def topology(self, controller_index: int) -> Optional[RaidTopology]:
        for topology in self.topologies:
            if topology.controller_index == controller_index:
                return topology
        return None

    def capabilities_for(self, controller_index: int) -> RaidCapabilities:
        return self.capabilities.get(controller_index) or RaidCapabilities()

    def to_json_obj(self) -> Dict[str, Any]:
        return {
            "tool": self.tool,
            "binary": self.binary,
            "collected_at": self.collected_at,
            "topologies": [topology.to_json_obj() for topology in self.topologies],
            "capabilities": {
                str(index): capabilities.to_json_obj() for index, capabilities in self.capabilities.items()
            },
            "error": self.error,
        }


def format_bytes(num: Optional[int]) -> str:
    """Render a byte count with decimal (SI) suffixes, e.g. ``1.82 TB``."""

    if not num:
        return "-"
    value = float(num)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if value < 1024.0 or unit == "PB":
            return f"{value:.2f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024.0
    return f"{value:.2f} PB"


def shlex_quote(value: str) -> str:
    """Quote an argv item for display purposes (safe on every Python 3)."""

    import shlex

    return shlex.quote(value)


def dumps(obj: Any) -> str:
    """Compact JSON dump helper used by the evidence writers."""

    return json.dumps(obj, ensure_ascii=False, sort_keys=False, default=str)
