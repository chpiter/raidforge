"""Adaptec / Microchip support (``arcconf``)."""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple

from ...config import Config
from ..models import (
    ControllerInfo,
    DiskInfo,
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

_FREE_STATES = {"READY"}
_CTRL_DETAIL_KEYS = (
    "ControllerName",
    "Model",
    "SerialNumber",
    "FirmwareVersion",
    "BIOSVersion",
    "PCIAddress",
    "ControllerStatus",
)


def _disk_id(controller_index: int, channel: int, device: int) -> str:
    return f"c{controller_index}-ch{channel}-dev{device}"


def _member_ids(record: Dict[str, Any], controller_index: int) -> List[str]:
    members: List[str] = []
    for node in walk_dicts(record):
        lowered = {str(key).lower(): key for key in node.keys()}
        if "channel" in lowered and any(key in lowered for key in ("device", "devicenumber", "device id", "deviceid")):
            channel = as_int(node.get(lowered["channel"]))
            device_key = next(
                (lowered[key] for key in ("device", "devicenumber", "device id", "deviceid") if key in lowered),
                None,
            )
            device = as_int(node.get(device_key)) if device_key else None
            if channel is not None and device is not None:
                members.append(_disk_id(controller_index, channel, device))
    return unique(members)


def _looks_like_ld(record: Dict[str, Any]) -> bool:
    lowered = {str(key).lower() for key in record.keys()}
    identity = any(
        key in lowered
        for key in ("logicaldrivenumber", "logicaldevicenumber", "logicaldevice", "logicaldrive", "ld")
    )
    details = any(key in lowered for key in ("raidlevel", "raid level", "level", "status", "size", "capacity"))
    return identity and details


def _ld_number(record: Dict[str, Any]) -> Optional[int]:
    return as_int(
        first_value(
            record,
            ("LogicalDeviceNumber", "LogicalDriveNumber", "LogicalDevice", "LogicalDrive", "LD", "Device"),
        )
    )


def _ld_array(record: Dict[str, Any]) -> str:
    return first_value(record, ("Array", "ArrayNumber", "Array ID", "ArrayId"))


def _make_volume(
    controller_index: int,
    number: int,
    record: Dict[str, Any],
    members: List[str],
    fallback_by_array: Dict[str, List[str]],
) -> Tuple[VolumeInfo, bool]:
    raw_level = first_value(record, ("RAIDLevel", "RaidLevel", "RAID Level", "Level"))
    level = normalize_level(raw_level)
    array = _ld_array(record)
    ambiguous = False
    if not members and array:
        members = list(fallback_by_array.get(array, []))
    if not members:
        ambiguous = True
    if level == "RAID10" and len(members) > 1:
        spans = [members[index : index + 2] for index in range(0, len(members), 2)]
    else:
        spans = [members] if members else []
    volume = VolumeInfo(
        volume_id=str(number),
        controller_index=controller_index,
        vendor=Vendor.PMC,
        level=level,
        raw_level=raw_level,
        name=first_value(record, ("LogicalDeviceName", "LogicalDriveName", "Name", "LDName", "Label")),
        state=first_value(record, ("Status", "StatusOfLogicalDevice", "State", "DeviceState")),
        size_bytes=parse_decimal_size(first_value(record, ("Size", "Capacity", "Size(MB)"))),
        disk_ids=list(members),
        spans=[list(span) for span in spans],
        disk_group=array,
        raw=dict(record),
    )
    return volume, ambiguous


def parse_ld_json(
    text: str, controller: ControllerInfo, disks: List[DiskInfo]
) -> Tuple[List[VolumeInfo], Dict[str, str], bool]:
    """Parse logical drives out of ``arcconf getconfigjson``."""

    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return [], {}, False

    fallback_by_array: Dict[str, List[str]] = {}
    for disk in disks:
        array = str(disk.raw.get("Array", "") or "").strip()
        if array:
            fallback_by_array.setdefault(array, []).append(disk.disk_id)

    records: Dict[int, Dict[str, Any]] = {}
    for node in walk_dicts(payload):
        if not _looks_like_ld(node):
            continue
        number = _ld_number(node)
        if number is not None and number not in records:
            records[number] = node

    volumes: List[VolumeInfo] = []
    membership: Dict[str, str] = {}
    ambiguous_any = False
    for number in sorted(records):
        record = records[number]
        volume, ambiguous = _make_volume(
            controller.index, number, record, _member_ids(record, controller.index), fallback_by_array
        )
        ambiguous_any = ambiguous_any or ambiguous
        volumes.append(volume)
        for disk_id in volume.disk_ids:
            membership.setdefault(disk_id, volume.volume_id)
    return volumes, membership, ambiguous_any


def parse_ld_text(
    text: str, controller: ControllerInfo, disks: List[DiskInfo]
) -> Tuple[List[VolumeInfo], Dict[str, str], bool]:
    """Fallback parser for ``arcconf getconfig <ctrl> ld``."""

    fallback_by_array: Dict[str, List[str]] = {}
    for disk in disks:
        array = str(disk.raw.get("Array", "") or "").strip()
        if array:
            fallback_by_array.setdefault(array, []).append(disk.disk_id)

    blocks = re.split(r"(?=^\s*Logical [Dd]evice number\s+\d+)", text, flags=re.MULTILINE)
    volumes: List[VolumeInfo] = []
    membership: Dict[str, str] = {}
    ambiguous_any = False
    for block in blocks:
        header = re.search(r"^\s*Logical [Dd]evice number\s+(\d+)", block, re.MULTILINE)
        if not header:
            continue
        number = int(header.group(1))
        fields: Dict[str, str] = {}
        for line in block.splitlines():
            if ":" in line:
                key, _, value = line.partition(":")
                fields[key.strip().lower()] = value.strip()
        raw_level = fields.get("raid level", "")
        level = normalize_level(raw_level)
        array = fields.get("array", "")
        members: List[str] = []
        for line in block.splitlines():
            if not re.search(r"segment|channel", line, re.IGNORECASE):
                continue
            for channel, device in re.findall(r"(\d+)\s*,\s*(\d+)", line):
                members.append(_disk_id(controller.index, int(channel), int(device)))
        members = unique(members)
        if not members and array:
            members = list(fallback_by_array.get(array, []))
        ambiguous = not members
        ambiguous_any = ambiguous_any or ambiguous
        if level == "RAID10" and len(members) > 1:
            spans = [members[index : index + 2] for index in range(0, len(members), 2)]
        else:
            spans = [members] if members else []
        volume = VolumeInfo(
            volume_id=str(number),
            controller_index=controller.index,
            vendor=Vendor.PMC,
            level=level,
            raw_level=raw_level,
            name=fields.get("logical device name", "") or fields.get("name", ""),
            state=fields.get("status of logical device", "") or fields.get("status", ""),
            size_bytes=parse_decimal_size(fields.get("size", "")),
            disk_ids=members,
            spans=[list(span) for span in spans],
            disk_group=array,
            raw={"block": block[:1000]},
        )
        volumes.append(volume)
        for disk_id in volume.disk_ids:
            membership.setdefault(disk_id, volume.volume_id)
    return volumes, membership, ambiguous_any


def controller_details(raw_json: Dict[str, Any]) -> Dict[str, str]:
    details: Dict[str, str] = {}
    for node in walk_dicts(raw_json or {}):
        for key in _CTRL_DETAIL_KEYS:
            if key in details:
                continue
            value = first_value(node, (key,))
            if value:
                details[key] = value
    return details


class AdaptecProvider(RaidProvider):
    """arcconf implementation of the RAID provider contract."""

    name = "adaptec"
    provider_key = "adaptec"

    def capabilities(self, controller: ControllerInfo) -> RaidCapabilities:
        return RaidCapabilities(
            levels=[RaidLevel.RAID0.value, RaidLevel.RAID1.value, RaidLevel.RAID5.value, RaidLevel.RAID10.value],
            create=True,
            delete_vd=True,
            clear_config=True,
            raid0_per_disk=True,
            name_supported=True,
            stripe_supported=True,
            notes=[
                "stripesize/name option order varies by firmware (VERIFY)",
                "clearconfig = delete all arrays + uninit all drives",
            ],
        )

    def collect(
        self, cfg: Config, controller: ControllerInfo, disks: List[DiskInfo]
    ) -> Tuple[RaidTopology, List[Tuple[str, str]]]:
        binary = self.binary or "arcconf"
        controller_disks = sort_disks(
            [disk for disk in disks if disk.controller_index == controller.index and disk.vendor is Vendor.PMC]
        )
        raw: List[Tuple[str, str]] = []
        result = run_cmd([binary, "getconfigjson", str(controller.index)], timeout=cfg.enum_timeout)
        raw.append((f"raid_pmc_c{controller.index}_getconfigjson.json", result.stdout))
        volumes, membership, ambiguous = parse_ld_json(result.stdout, controller, controller_disks)
        if not volumes:
            legacy = run_cmd([binary, "getconfig", str(controller.index), "ld"], timeout=cfg.enum_timeout)
            raw.append((f"raid_pmc_c{controller.index}_getconfig_ld.txt", legacy.stdout))
            volumes, membership, ambiguous = parse_ld_text(legacy.stdout, controller, controller_disks)

        controller.details.update(controller_details(controller.raw_json))

        blocked: Dict[str, str] = {}
        free: List[str] = []
        states: Dict[str, str] = {}
        for disk in controller_disks:
            state = str(disk.raid_state or disk.raw.get("State", "") or "").strip()
            states[disk.disk_id] = state
            array = str(disk.raw.get("Array", "") or "").strip()
            if ambiguous:
                blocked[disk.disk_id] = "logical device membership unparsable - refusing to guess"
            elif disk.disk_id in membership:
                blocked[disk.disk_id] = f"member of logical device {membership[disk.disk_id]}"
            elif state.upper() not in _FREE_STATES:
                blocked[disk.disk_id] = f"state {state or 'unknown'!r} is not free (need Ready)"
            elif array:
                blocked[disk.disk_id] = f"array {array} present but no logical device mapping"
            elif not addressable(disk, Vendor.PMC):
                blocked[disk.disk_id] = "no channel/device address"
            elif not disk.size_bytes:
                blocked[disk.disk_id] = "unknown capacity"
            else:
                free.append(disk.disk_id)

        error = ""
        if not result.ok and not volumes:
            error = f"arcconf getconfigjson failed: rc={result.return_code} {result.stderr[-200:]}".strip()

        topology = RaidTopology(
            controller_index=controller.index,
            vendor=Vendor.PMC,
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


__all__ = ["AdaptecProvider", "controller_details", "parse_ld_json", "parse_ld_text"]
