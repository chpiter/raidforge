"""LSI / Broadcom MegaRAID support (``storcli`` and ``perccli``)."""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Set, Tuple

from ...config import Config
from ..enum_disks import parse_storcli_json
from ..models import (
    ControllerInfo,
    DiskInfo,
    RaidAction,
    RaidCapabilities,
    RaidCommandPlan,
    RaidLayout,
    RaidLevel,
    RaidTopology,
    Vendor,
    VolumeInfo,
)
from ..utils import parse_decimal_size, run_cmd
from .base import RaidProvider
from .commands import clear_plans, create_plans, delete_plans
from .layout import addressable, sort_disks
from .parsing import as_int, first_value, normalize_level, unique, walk_dicts

_VD_KEY_RE = re.compile(r"VD\s*/c(\d+)/v(\d+)", re.IGNORECASE)
_PDS_KEY_RE = re.compile(r"PDs for VD\s+(\d+)", re.IGNORECASE)
_DRIVE_KEY_RE = re.compile(r"Drive\s*/c(\d+)/e(\d+)/s(\d+)", re.IGNORECASE)
_REF_RE = re.compile(r"(\d+)\s*[:,]\s*(\d+)")

_FREE_STATES = {"UG", "UGOOD", "UNCONFIGURED GOOD"}
_DETAIL_KEYS = (
    "Product Name",
    "Serial Number",
    "FW Version",
    "Firmware Version",
    "SAS Address",
    "PCI Address",
    "Number of Ports",
    "Cache Memory Size",
    "BBU",
    "Controller Status",
    "Drive Groups",
    "Virtual Drives",
    "Physical Drives",
)


def _disk_id(controller_index: int, enclosure: int, slot: int) -> str:
    return f"c{controller_index}e{enclosure}s{slot}"


def _parse_ref(text: str, controller_index: int) -> Optional[str]:
    match = _REF_RE.search(str(text or ""))
    if not match:
        return None
    return _disk_id(controller_index, int(match.group(1)), int(match.group(2)))


def _vd_number(record: Dict[str, Any]) -> Optional[int]:
    dg_vd = first_value(record, ("DG/VD", "DG / VD", "DG:VD"))
    if "/" in dg_vd:
        return as_int(dg_vd.split("/")[-1])
    return as_int(first_value(record, ("VD", "VD ID", "Virtual Drive")))


def _record_members(record: Dict[str, Any], controller_index: int) -> List[str]:
    members: List[str] = []
    for key in ("Drives", "PDs", "Physical Drives", "Members"):
        value = record.get(key)
        if isinstance(value, str):
            members.extend(
                item
                for item in (_parse_ref(part, controller_index) for part in value.split(","))
                if item
            )
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    ref = first_value(item, ("EID:Slt", "EID/Slt", "Drive", "PD"))
                    parsed = _parse_ref(ref, controller_index) if ref else None
                    if parsed:
                        members.append(parsed)
                else:
                    parsed = _parse_ref(str(item), controller_index)
                    if parsed:
                        members.append(parsed)
    return unique(members)


def _make_volume(
    controller_index: int,
    number: int,
    record: Dict[str, Any],
    members: List[str],
    disks_by_id: Dict[str, DiskInfo],
    fallback_by_dg: Dict[str, List[str]],
) -> Tuple[VolumeInfo, bool]:
    raw_level = first_value(record, ("TYPE", "RAID Level", "Raid Level", "Type", "Level"))
    level = normalize_level(raw_level)
    dg = first_value(record, ("DG", "Disk Group", "Array"))
    ambiguous = False
    if not members:
        if dg and dg not in ("-", ""):
            members = list(fallback_by_dg.get(dg, []))
        if not members:
            ambiguous = True

    if level == "RAID10" and len(members) > 1:
        spans = [members[index : index + 2] for index in range(0, len(members), 2)]
    else:
        spans = [members] if members else []

    state = first_value(record, ("State", "Status", "State "))
    size = parse_decimal_size(first_value(record, ("Size", "Capacity", "Total Size")))
    name = first_value(record, ("Name", "VD Name", "Volume Name"))
    volume = VolumeInfo(
        volume_id=f"v{number}",
        controller_index=controller_index,
        vendor=Vendor.LSI,
        level=level,
        raw_level=raw_level,
        name=name,
        state=state,
        size_bytes=size,
        disk_ids=list(members),
        spans=[list(span) for span in spans],
        disk_group=dg,
        raw=dict(record),
    )
    return volume, ambiguous


def parse_vall_json(
    text: str, controller: ControllerInfo, disks: List[DiskInfo]
) -> Tuple[List[VolumeInfo], Dict[str, str], bool]:
    """Parse ``storcli /cN/vall show all J``.

    Returns ``(volumes, membership, ambiguous)``.  ``ambiguous`` is True when a
    virtual drive has no resolvable members; callers must then block every disk
    on that controller instead of guessing.
    """

    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return [], {}, False

    records: Dict[int, Dict[str, Any]] = {}
    pds: Dict[int, List[str]] = {}
    for node in walk_dicts(payload):
        for key, value in node.items():
            vd_match = _VD_KEY_RE.search(str(key))
            if vd_match and isinstance(value, dict):
                records[int(vd_match.group(2))] = value
            pds_match = _PDS_KEY_RE.search(str(key))
            if pds_match and isinstance(value, list):
                refs = []
                for item in value:
                    if isinstance(item, dict):
                        ref = first_value(item, ("EID:Slt", "EID/Slt"))
                        if ref:
                            refs.append(ref)
                    else:
                        refs.append(str(item))
                pds[int(pds_match.group(1))] = refs

    # Some firmware variants expose a "Virtual Drives" list without the
    # "VD /cX/vY" wrapper.  Accept records that carry DG/VD plus TYPE/State.
    for node in walk_dicts(payload):
        if first_value(node, ("DG/VD", "DG / VD")) and first_value(node, ("TYPE", "RAID Level")):
            number = _vd_number(node)
            if number is not None and number not in records:
                records[number] = node

    disks_by_id = {disk.disk_id: disk for disk in disks}
    fallback_by_dg: Dict[str, List[str]] = {}
    for disk in disks:
        dg = str(disk.raw.get("DG", "") or "").strip()
        if dg and dg != "-":
            fallback_by_dg.setdefault(dg, []).append(disk.disk_id)

    volumes: List[VolumeInfo] = []
    membership: Dict[str, str] = {}
    ambiguous_any = False
    for number in sorted(records):
        record = records[number]
        members = [_parse_ref(ref, controller.index) for ref in pds.get(number, [])]
        members = [member for member in members if member]
        if not members:
            members = _record_members(record, controller.index)
        volume, ambiguous = _make_volume(
            controller.index, number, record, members, disks_by_id, fallback_by_dg
        )
        ambiguous_any = ambiguous_any or ambiguous
        volumes.append(volume)
        for disk_id in volume.disk_ids:
            membership.setdefault(disk_id, volume.volume_id)
    return volumes, membership, ambiguous_any


def parse_vall_text(
    text: str, controller: ControllerInfo, disks: List[DiskInfo]
) -> Tuple[List[VolumeInfo], Dict[str, str], bool]:
    """Fallback parser for the plain ``storcli /cN/vall show`` table."""

    pds: Dict[int, List[str]] = {}
    current_vd: Optional[int] = None
    for line in text.splitlines():
        pds_match = re.match(r"\s*PDs for VD\s+(\d+)", line, re.IGNORECASE)
        if pds_match:
            current_vd = int(pds_match.group(1))
            pds.setdefault(current_vd, [])
            continue
        if current_vd is not None:
            ref = _parse_ref(line, controller.index)
            if ref and re.search(r"\d+\s*:\s*\d+", line):
                pds.setdefault(current_vd, []).append(ref)
            elif line.strip() and not line.strip().startswith("-"):
                current_vd = None

    fallback_by_dg: Dict[str, List[str]] = {}
    for disk in disks:
        dg = str(disk.raw.get("DG", "") or "").strip()
        if dg and dg != "-":
            fallback_by_dg.setdefault(dg, []).append(disk.disk_id)

    volumes: List[VolumeInfo] = []
    membership: Dict[str, str] = {}
    ambiguous_any = False
    for line in text.splitlines():
        match = re.match(
            r"\s*(\d+)\s*/\s*(\d+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(.+)$",
            line,
        )
        if not match:
            continue
        dg, vd, level_raw, state = match.group(1), int(match.group(2)), match.group(3), match.group(4)
        size_raw = match.group(10)
        name = match.group(11).strip()
        members = pds.get(vd, []) or fallback_by_dg.get(dg, [])
        ambiguous = not members
        ambiguous_any = ambiguous_any or ambiguous
        level = normalize_level(level_raw)
        if level == "RAID10" and len(members) > 1:
            spans = [members[index : index + 2] for index in range(0, len(members), 2)]
        else:
            spans = [members] if members else []
        volume = VolumeInfo(
            volume_id=f"v{vd}",
            controller_index=controller.index,
            vendor=Vendor.LSI,
            level=level,
            raw_level=level_raw,
            name=name,
            state=state,
            size_bytes=parse_decimal_size(size_raw),
            disk_ids=list(members),
            spans=[list(span) for span in spans],
            disk_group=dg,
            raw={"line": line},
        )
        volumes.append(volume)
        for disk_id in volume.disk_ids:
            membership.setdefault(disk_id, volume.volume_id)
    return volumes, membership, ambiguous_any


def controller_details(raw_json: Dict[str, Any]) -> Dict[str, str]:
    """Extract a small, display-friendly set of controller fields."""

    details: Dict[str, str] = {}
    for node in walk_dicts(raw_json or {}):
        for key in _DETAIL_KEYS:
            if key in details:
                continue
            value = first_value(node, (key,))
            if value:
                details[key] = value
        if len(details) == len(_DETAIL_KEYS):
            break
    return details


class LsiProvider(RaidProvider):
    """storcli/perccli implementation of the RAID provider contract."""

    name = "lsi"
    provider_key = "lsi"

    def capabilities(self, controller: ControllerInfo) -> RaidCapabilities:
        return RaidCapabilities(
            levels=[RaidLevel.RAID0.value, RaidLevel.RAID1.value, RaidLevel.RAID5.value, RaidLevel.RAID10.value],
            create=True,
            delete_vd=True,
            clear_config=True,
            raid0_per_disk=True,
            name_supported=True,
            stripe_supported=True,
            notes=["clearconfig = delete all VDs + delete foreign configuration"],
        )

    def collect(
        self, cfg: Config, controller: ControllerInfo, disks: List[DiskInfo]
    ) -> Tuple[RaidTopology, List[Tuple[str, str]]]:
        binary = self.binary or "storcli"
        controller_disks = sort_disks(
            [disk for disk in disks if disk.controller_index == controller.index and disk.vendor is Vendor.LSI]
        )
        raw: List[Tuple[str, str]] = []
        result = run_cmd(
            [binary, f"/c{controller.index}/vall", "show", "all", "J"], timeout=cfg.enum_timeout
        )
        raw.append((f"raid_lsi_c{controller.index}_vall_show_all_J.json", result.stdout))
        volumes, membership, ambiguous = parse_vall_json(result.stdout, controller, controller_disks)
        if not volumes:
            text = run_cmd([binary, f"/c{controller.index}/vall", "show"], timeout=cfg.enum_timeout)
            raw.append((f"raid_lsi_c{controller.index}_vall_show.txt", text.stdout))
            volumes, membership, ambiguous = parse_vall_text(text.stdout, controller, controller_disks)

        controller.details.update(controller_details(controller.raw_json))

        blocked: Dict[str, str] = {}
        free: List[str] = []
        states: Dict[str, str] = {}
        for disk in controller_disks:
            state = str(disk.raid_state or disk.raw.get("State", "") or "").strip()
            states[disk.disk_id] = state
            dg = str(disk.raw.get("DG", "") or "").strip()
            if ambiguous:
                blocked[disk.disk_id] = "VD membership unparsable - refusing to guess"
            elif disk.disk_id in membership:
                blocked[disk.disk_id] = f"member of {membership[disk.disk_id]}"
            elif state.upper() not in _FREE_STATES:
                blocked[disk.disk_id] = f"state {state or 'unknown'!r} is not free (need UGood)"
            elif dg not in ("", "-"):
                blocked[disk.disk_id] = f"disk group {dg} present but no VD mapping"
            elif not addressable(disk, Vendor.LSI):
                blocked[disk.disk_id] = "no enclosure/slot address"
            elif not disk.size_bytes:
                blocked[disk.disk_id] = "unknown capacity"
            else:
                free.append(disk.disk_id)

        error = ""
        if not result.ok and not volumes:
            error = f"storcli vall failed: rc={result.return_code} {result.stderr[-200:]}".strip()

        topology = RaidTopology(
            controller_index=controller.index,
            vendor=Vendor.LSI,
            tool=controller.tool or self.name,
            binary=binary,
            volumes=volumes,
            membership=membership,
            free_disk_ids=free,
            blocked=blocked,
            disk_states=states,
            raw_files=[name for name, _ in raw],
            error=error,
        )
        return topology, raw

    def create_plans(self, controller: ControllerInfo, layout: RaidLayout) -> List[RaidCommandPlan]:
        return create_plans(controller, layout)

    def delete_plans(self, controller: ControllerInfo, volumes: List[VolumeInfo]) -> List[RaidCommandPlan]:
        return delete_plans(controller, volumes)

    def clear_plans(self, controller: ControllerInfo) -> List[RaidCommandPlan]:
        return clear_plans(controller)


__all__ = [
    "LsiProvider",
    "controller_details",
    "parse_vall_json",
    "parse_vall_text",
]
