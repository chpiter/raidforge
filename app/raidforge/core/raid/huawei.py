"""Huawei HiRAID (hiraidadm) RAID provider."""

from __future__ import annotations

from typing import Dict, List, Tuple

from ...config import Config
from ..hiraidadm import parse_kv, parse_pdarray, vd_ids
from ..models import (
    ControllerInfo,
    DiskInfo,
    RaidCapabilities,
    RaidLevel,
    RaidTopology,
    Vendor,
    VolumeInfo,
)
from ..utils import parse_decimal_size, run_cmd
from .base import RaidProvider
from .commands import clear_plans, create_plans, delete_plans
from .layout import addressable, sort_disks
from .parsing import normalize_level, unique

#: Physical-drive states that are safe to select for erase.  HiRAID reports
#: "Unconfigured Good" for spare members; anything else (Online, Rebuild,
#: Failed, Foreign, ...) is blocked.  Refine only after capturing real output.
_FREE_STATES = {
    "UNCONFIGURED GOOD",
    "UNCONFIGUREDGOOD",
    "UNCONFIGURED",
    "UG",
    "UGOOD",
    "READY",
    "FREE",
    "AVAILABLE",
}

_EMPTY_GROUP = {"", "-", "NA", "N/A", "NONE"}


def _disk_id(controller: ControllerInfo, enc: str, slot: str) -> str:
    return f"c{controller.index}e{enc}s{slot}"


def _group_of(disk: DiskInfo) -> str:
    for key in ("raid group id", "raid group", "rg", "raidgroup"):
        value = disk.raw.get(key)
        if value is not None:
            return str(value).strip()
    return ""


def _prefail(disk: DiskInfo) -> bool:
    value = str(disk.raw.get("prefail", "") or disk.raw.get("pre-fail", "")).strip().upper()
    return value in {"YES", "Y", "TRUE", "1"}


class HuaweiProvider(RaidProvider):
    """hiraidadm implementation of the RAID provider contract."""

    name = "huawei"
    provider_key = "huawei"

    def capabilities(self, controller: ControllerInfo) -> RaidCapabilities:
        return RaidCapabilities(
            levels=[
                RaidLevel.RAID0.value,
                RaidLevel.RAID1.value,
                RaidLevel.RAID5.value,
                RaidLevel.RAID10.value,
            ],
            create=True,
            delete_vd=True,
            clear_config=True,
            raid0_per_disk=True,
            name_supported=True,
            stripe_supported=False,
            notes=[
                "sanitize maps to `start sanitize mode=cryptoerase` and needs a SED; "
                "plain SSDs use blockerase",
                "clearconfig = reset defaultconfig + clear fncfg (VERIFY on hardware)",
                "RAID6/50/60 are not exposed by the UI",
            ],
        )

    def collect(
        self, cfg: Config, controller: ControllerInfo, disks: List[DiskInfo]
    ) -> Tuple[RaidTopology, List[Tuple[str, str]]]:
        binary = self.binary or "hiraidadm"
        index = controller.index
        controller_disks = sort_disks(
            [
                disk
                for disk in disks
                if disk.controller_index == controller.index and disk.vendor is Vendor.HUAWEI
            ]
        )
        raw: List[Tuple[str, str]] = []

        pd_result = run_cmd([binary, f"c{index}", "show", "pdlist"], timeout=cfg.enum_timeout)
        raw.append((f"raid_huawei_c{index}_pdlist.txt", pd_result.stdout))
        vd_result = run_cmd([binary, f"c{index}", "show", "vdlist"], timeout=cfg.enum_timeout)
        raw.append((f"raid_huawei_c{index}_vdlist.txt", vd_result.stdout))

        info = run_cmd([binary, f"c{index}", "show"], timeout=cfg.enum_timeout)
        raw.append((f"raid_huawei_c{index}_show.txt", info.stdout))
        fields = parse_kv(info.stdout)
        for key in ("model", "serial number", "firmware version", "sas address"):
            value = fields.get(key, "")
            if value:
                controller.details[key.replace(" ", "_")] = value

        volumes: List[VolumeInfo] = []
        membership: Dict[str, str] = {}
        ambiguous_any = False
        for volume_id in vd_ids(vd_result.stdout):
            address = f"c{index}:vd{volume_id}"
            detail = run_cmd([binary, address, "show"], timeout=cfg.enum_timeout)
            raw.append((f"raid_huawei_c{index}_vd{volume_id}_show.txt", detail.stdout))
            vd_fields = parse_kv(detail.stdout)
            member_result = run_cmd([binary, address, "show", "pdarray"], timeout=cfg.enum_timeout)
            raw.append((f"raid_huawei_c{index}_vd{volume_id}_pdarray.txt", member_result.stdout))
            members = unique(
                _disk_id(controller, row.get("enc", ""), row.get("slot", ""))
                for row in parse_pdarray(member_result.stdout)
                if str(row.get("enc", "")).isdigit() and str(row.get("slot", "")).isdigit()
            )
            ambiguous = not members
            ambiguous_any = ambiguous_any or ambiguous
            level = normalize_level(vd_fields.get("raid level", "") or vd_fields.get("raid id", ""))
            if level == "RAID10" and len(members) > 1:
                spans = [members[position : position + 2] for position in range(0, len(members), 2)]
            else:
                spans = [members] if members else []
            volume = VolumeInfo(
                volume_id=f"vd{volume_id}",
                controller_index=index,
                vendor=Vendor.HUAWEI,
                level=level,
                raw_level=vd_fields.get("raid level", ""),
                name=vd_fields.get("name", ""),
                state=vd_fields.get("status", ""),
                size_bytes=parse_decimal_size(vd_fields.get("capacity", "")),
                disk_ids=members,
                spans=[list(span) for span in spans],
                raw={"fields": vd_fields, "members": members},
            )
            volumes.append(volume)
            for disk_id in members:
                membership.setdefault(disk_id, volume.volume_id)

        blocked: Dict[str, str] = {}
        free: List[str] = []
        states: Dict[str, str] = {}
        for disk in controller_disks:
            state = str(disk.raid_state or "").strip()
            states[disk.disk_id] = state
            group = _group_of(disk)
            if ambiguous_any:
                blocked[disk.disk_id] = "VD membership unparsable - refusing to guess"
            elif disk.disk_id in membership:
                blocked[disk.disk_id] = f"member of {membership[disk.disk_id]}"
            elif state.upper() not in _FREE_STATES:
                blocked[disk.disk_id] = f"state {state or 'unknown'!r} is not free"
            elif _prefail(disk):
                blocked[disk.disk_id] = "controller marks the drive as prefail"
            elif group not in _EMPTY_GROUP:
                blocked[disk.disk_id] = f"RAID group {group} present but no VD mapping"
            elif not addressable(disk, Vendor.HUAWEI):
                blocked[disk.disk_id] = "no enclosure/slot address"
            elif not disk.size_bytes:
                blocked[disk.disk_id] = "unknown capacity"
            else:
                free.append(disk.disk_id)

        error = ""
        if not pd_result.ok and not controller_disks:
            error = f"hiraidadm pdlist failed: rc={pd_result.return_code} {pd_result.stderr[-200:]}".strip()
        elif not vd_result.ok and not volumes:
            error = f"hiraidadm vdlist failed: rc={vd_result.return_code} {vd_result.stderr[-200:]}".strip()

        topology = RaidTopology(
            controller_index=index,
            vendor=Vendor.HUAWEI,
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

    def create_plans(self, controller: ControllerInfo, layout):
        return create_plans(controller, layout)

    def delete_plans(self, controller: ControllerInfo, volumes: List[VolumeInfo]):
        return delete_plans(controller, volumes)

    def clear_plans(self, controller: ControllerInfo):
        return clear_plans(controller)


__all__ = ["HuaweiProvider"]
