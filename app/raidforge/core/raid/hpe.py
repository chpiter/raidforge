"""HPE Smart Array support (``ssacli`` and legacy ``hpssacli``)."""

from __future__ import annotations

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
from .parsing import first_value, normalize_level, unique

_LD_HEADER_RE = re.compile(
    r"^\s*logical\s*drive\s+(\d+)(?:\s*\(([^)]*)\))?", re.IGNORECASE | re.MULTILINE
)
_PD_RE = re.compile(r"physicaldrive\s+(\S+)", re.IGNORECASE)
_SECTION_RE = re.compile(r"^\s*(unassigned|Array\s+\S+)\s*$", re.IGNORECASE)
_DETAIL_KEYS = (
    "Model",
    "Firmware Revision",
    "Serial Number",
    "Controller Status",
    "Cache Module Status",
    "Battery/Capacitor Status",
    "PCI Address",
    "Drive Enclosures",
    "Drive Bays",
)


def _clean_ref(value: str) -> str:
    return str(value or "").strip().rstrip(",")


def _disk_id(controller_index: int, ref: str) -> str:
    return f"c{controller_index}-{ref}"


def _header_parts(header_content: str) -> Dict[str, str]:
    parts = [part.strip() for part in str(header_content or "").split(",") if part.strip()]
    result = {"size": parts[0] if parts else ""}
    for part in parts[1:]:
        upper = part.upper()
        if "RAID" in upper or upper in {"0", "1", "5", "6", "1+0"}:
            result["level"] = part
        elif upper in {"OK", "FAILED", "DEGRADED", "REBUILDING", "INTERIM RECOVERY"}:
            result["status"] = part
    return result


def _member_refs(block: str) -> List[str]:
    refs = [_clean_ref(match.group(1)) for match in _PD_RE.finditer(block)]
    return unique(ref for ref in refs if ref)


def parse_ld_detail(
    text: str, controller: ControllerInfo, disks: List[DiskInfo]
) -> Tuple[List[VolumeInfo], Dict[str, str], bool]:
    """Parse ``ssacli ctrl slot=N ld all show detail``."""

    starts = [match.start() for match in _LD_HEADER_RE.finditer(text)]
    blocks: List[str] = []
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else len(text)
        blocks.append(text[start:end])

    volumes: List[VolumeInfo] = []
    membership: Dict[str, str] = {}
    ambiguous_any = False
    for block in blocks:
        header = _LD_HEADER_RE.search(block)
        if not header:
            continue
        number = int(header.group(1))
        header_fields = _header_parts(header.group(2) or "")
        fields: Dict[str, str] = {}
        for line in block.splitlines():
            if ":" in line:
                key, _, value = line.partition(":")
                fields[key.strip().lower()] = value.strip()

        raw_level = fields.get("fault tolerance", "") or header_fields.get("level", "")
        level = normalize_level(raw_level)
        refs = _member_refs(block)
        ambiguous = not refs
        ambiguous_any = ambiguous_any or ambiguous
        members = [_disk_id(controller.index, ref) for ref in refs]
        if level == "RAID10" and len(members) > 1:
            spans = [members[index : index + 2] for index in range(0, len(members), 2)]
        else:
            spans = [members] if members else []
        volume = VolumeInfo(
            volume_id=str(number),
            controller_index=controller.index,
            vendor=Vendor.HPE,
            level=level,
            raw_level=raw_level,
            name=fields.get("logical drive label", "") or fields.get("label", ""),
            state=fields.get("status", "") or header_fields.get("status", ""),
            size_bytes=parse_decimal_size(fields.get("size", "") or header_fields.get("size", "")),
            disk_ids=members,
            spans=[list(span) for span in spans],
            disk_group=fields.get("array", ""),
            raw={"header": header.group(0).strip(), "block": block[:1000]},
        )
        volumes.append(volume)
        for disk_id in volume.disk_ids:
            membership.setdefault(disk_id, volume.volume_id)
    return volumes, membership, ambiguous_any


def parse_pd_groups(text: str) -> Dict[str, str]:
    """Map a physical-drive reference to ``unassigned`` or ``Array X``."""

    groups: Dict[str, str] = {}
    section = ""
    for line in text.splitlines():
        section_match = _SECTION_RE.match(line)
        if section_match:
            section = "unassigned" if section_match.group(1).lower() == "unassigned" else section_match.group(1).strip()
            continue
        pd_match = _PD_RE.search(line)
        if pd_match and section:
            groups[_clean_ref(pd_match.group(1))] = section
    return groups


def parse_detail_fields(text: str) -> Dict[str, str]:
    """Extract display-friendly controller fields from ``ssacli ... show detail``."""

    fields: Dict[str, str] = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    return fields


def controller_details(fields: Dict[str, str]) -> Dict[str, str]:
    details: Dict[str, str] = {}
    for key in _DETAIL_KEYS:
        value = first_value(fields, (key,))
        if value:
            details[key] = value
    return details


class HpeProvider(RaidProvider):
    """ssacli/hpssacli implementation of the RAID provider contract."""

    name = "hpe"
    provider_key = "hpe"

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
                "RAID10 uses raid=1+0",
                "clearconfig = ld all delete + modify clearconfig (VERIFY firmware behaviour)",
            ],
        )

    def collect(
        self, cfg: Config, controller: ControllerInfo, disks: List[DiskInfo]
    ) -> Tuple[RaidTopology, List[Tuple[str, str]]]:
        binary = self.binary or "ssacli"
        slot = controller.index
        controller_disks = sort_disks(
            [disk for disk in disks if disk.controller_index == controller.index and disk.vendor is Vendor.HPE]
        )
        raw: List[Tuple[str, str]] = []

        result = run_cmd(
            [binary, "ctrl", f"slot={slot}", "ld", "all", "show", "detail"], timeout=cfg.enum_timeout
        )
        raw.append((f"raid_hpe_c{slot}_ld_all_show_detail.txt", result.stdout))
        volumes, membership, ambiguous = parse_ld_detail(result.stdout, controller, controller_disks)
        if not volumes or ambiguous:
            # ``show config detail`` nests Array -> Logical Drive -> physicaldrive
            # and is available on older firmware where the flat LD listing omits
            # members.
            config = run_cmd(
                [binary, "ctrl", f"slot={slot}", "show", "config", "detail"], timeout=cfg.enum_timeout
            )
            raw.append((f"raid_hpe_c{slot}_show_config_detail.txt", config.stdout))
            fallback_volumes, fallback_membership, fallback_ambiguous = parse_ld_detail(
                config.stdout, controller, controller_disks
            )
            if fallback_volumes:
                volumes, membership, ambiguous = fallback_volumes, fallback_membership, fallback_ambiguous

        pd_listing = run_cmd([binary, "ctrl", f"slot={slot}", "pd", "all", "show"], timeout=cfg.enum_timeout)
        raw.append((f"raid_hpe_c{slot}_pd_all_show.txt", pd_listing.stdout))
        pd_groups = parse_pd_groups(pd_listing.stdout)

        info = run_cmd([binary, "ctrl", f"slot={slot}", "show", "detail"], timeout=cfg.enum_timeout)
        raw.append((f"raid_hpe_c{slot}_show_detail.txt", info.stdout))
        controller.details.update(controller_details(parse_detail_fields(info.stdout)))

        blocked: Dict[str, str] = {}
        free: List[str] = []
        states: Dict[str, str] = {}
        for disk in controller_disks:
            ref = disk.controller_ref
            state = str(disk.raid_state or "").strip()
            states[disk.disk_id] = state
            group = pd_groups.get(ref, "") if ref else ""
            if ambiguous:
                blocked[disk.disk_id] = "logical drive membership unparsable - refusing to guess"
            elif not pd_groups:
                blocked[disk.disk_id] = "`pd all show` grouping unavailable - eligibility unknown"
            elif disk.disk_id in membership:
                blocked[disk.disk_id] = f"member of logical drive {membership[disk.disk_id]}"
            elif group.lower() != "unassigned":
                blocked[disk.disk_id] = f"group {group or 'unknown'} is not unassigned"
            elif state.upper() != "OK":
                blocked[disk.disk_id] = f"status {state or 'unknown'!r} is not OK"
            elif not addressable(disk, Vendor.HPE):
                blocked[disk.disk_id] = "no controller reference"
            elif not disk.size_bytes:
                blocked[disk.disk_id] = "unknown capacity"
            else:
                free.append(disk.disk_id)

        error = ""
        if not result.ok and not volumes:
            error = f"ssacli ld listing failed: rc={result.return_code} {result.stderr[-200:]}".strip()
        elif not pd_listing.ok and not pd_groups:
            error = f"ssacli pd listing failed: rc={pd_listing.return_code} {pd_listing.stderr[-200:]}".strip()

        topology = RaidTopology(
            controller_index=controller.index,
            vendor=Vendor.HPE,
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


__all__ = ["HpeProvider", "controller_details", "parse_ld_detail", "parse_pd_groups"]
