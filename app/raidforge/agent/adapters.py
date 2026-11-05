"""任务参数 → core 计划/结果 的适配层。"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from ..config import Config
from ..core.commands import available_methods, build_command
from ..core.models import (
    CommandPlan,
    ControllerInfo,
    DiskInfo,
    Raid0Mode,
    RaidCommandPlan,
    RaidLayout,
    RaidLevel,
    VolumeInfo,
)
from ..core.raid import provider_for
from ..core.raid.layout import build_layout, ensure_valid
from .models import RemoteTask, RemoteTaskType, TaskRejected
from . import safety
from .snapshot import HostSnapshot
from .topology import normalize_wwn

log = logging.getLogger(__name__)


@dataclass
class CreatePlan:
    controller: ControllerInfo
    layout: RaidLayout
    plans: List[RaidCommandPlan]
    warnings: List[str] = field(default_factory=list)

    def previews(self) -> List[str]:
        return [plan.dry_run_text for plan in self.plans]


@dataclass
class DeletePlan:
    controller: ControllerInfo
    volume: VolumeInfo
    plans: List[RaidCommandPlan]
    warnings: List[str] = field(default_factory=list)

    def previews(self) -> List[str]:
        return [plan.dry_run_text for plan in self.plans]


@dataclass
class ClearPlan:
    controller: ControllerInfo
    plans: List[RaidCommandPlan]
    vd_count: int
    warnings: List[str] = field(default_factory=list)

    def previews(self) -> List[str]:
        return [plan.dry_run_text for plan in self.plans]


@dataclass
class ErasePlan:
    controller: ControllerInfo
    disks: List[DiskInfo]
    plans: List[CommandPlan]
    passes: int
    eta_seconds: int
    warnings: List[str] = field(default_factory=list)

    def previews(self) -> List[str]:
        return [plan.dry_run_text for plan in self.plans]


def _controller(snapshot: HostSnapshot, controller_id: str) -> tuple[ControllerInfo, Dict[str, Any], Dict[str, Any]]:
    metadata = snapshot.controller(controller_id)
    if metadata is None:
        raise TaskRejected("controller_not_found", f"控制器 {controller_id} 不存在")
    info = snapshot.controller_info(controller_id)
    if info is None:
        raise TaskRejected("controller_not_found", f"控制器 {controller_id} 不存在")
    return info, metadata, snapshot.capabilities_for(controller_id)


def _pd(snapshot: HostSnapshot, disk: DiskInfo) -> Dict[str, Any]:
    for item in snapshot.pds:
        if item.get("disk_id") == disk.disk_id:
            return item
    return {}


def select_disks(snapshot: HostSnapshot, controller_id: str, selectors: Sequence[Dict[str, Any]]) -> List[DiskInfo]:
    info, _metadata, _caps = _controller(snapshot, controller_id)
    if snapshot.scan is None:
        raise TaskRejected("controller_not_found", "缺少扫描结果")
    candidates = [disk for disk in snapshot.scan.disks if disk.controller_index == info.index]
    chosen: List[DiskInfo] = []
    seen: set[str] = set()
    for selector in selectors:
        serial = str(selector.get("serial") or "")
        wwn = normalize_wwn(selector.get("wwn"))
        size = int(selector.get("expected_size_bytes") or 0)
        if not serial or not wwn:
            raise TaskRejected("disk_identity_insufficient", "目标选择器缺少 serial 或 wwn", field="pds")
        by_serial = [disk for disk in candidates if str(disk.serial or "").casefold() == serial.casefold()]
        by_wwn = [disk for disk in candidates if normalize_wwn(disk.wwn) == wwn]
        pool = by_serial or by_wwn
        if not pool:
            raise TaskRejected(
                "disk_not_found",
                f"未找到 serial={serial!r} / wwn={wwn!r} 的磁盘",
                detail={"selector": selector},
            )
        matches = [disk for disk in pool if normalize_wwn(disk.wwn) == wwn and int(disk.size_bytes or 0) == size]
        if len(matches) != 1:
            raise TaskRejected(
                "disk_identity_mismatch",
                f"serial/wwn/size 未能唯一匹配 {serial!r}",
                detail={"selector": selector, "candidates": [disk.to_json_obj() for disk in pool]},
            )
        disk = matches[0]
        if disk.disk_id in seen:
            raise TaskRejected("invalid_params", f"重复目标 {disk.disk_id}", field="pds")
        seen.add(disk.disk_id)
        chosen.append(disk)
    return chosen


def select_volume(
    snapshot: HostSnapshot,
    controller_id: str,
    vd_id: str,
    vd_name: str,
    expected_size: int,
) -> VolumeInfo:
    info, _metadata, _caps = _controller(snapshot, controller_id)
    if snapshot.scan is None:
        raise TaskRejected("controller_not_found", "缺少扫描结果")
    topology = snapshot.scan.raid.topology(info.index)
    if topology is None:
        raise TaskRejected("controller_not_found", f"控制器 c{info.index} 无拓扑")
    for volume in topology.volumes:
        if volume.volume_id == vd_id:
            if volume.name != vd_name or int(volume.size_bytes or 0) != expected_size:
                raise TaskRejected(
                    "vd_identity_mismatch",
                    "vd_id 对应的名称或容量不匹配",
                    detail={
                        "vd_id": vd_id,
                        "expected_name": vd_name,
                        "actual_name": volume.name,
                        "expected_size": expected_size,
                        "actual_size": volume.size_bytes,
                    },
                )
            return volume
    raise TaskRejected("vd_not_found", f"VD {vd_id} 不存在")


def select_controllers(snapshot: HostSnapshot, controller_ids: Optional[Sequence[str]]) -> List[Dict[str, Any]]:
    if not controller_ids:
        return list(snapshot.controllers)
    selected: List[Dict[str, Any]] = []
    for controller_id in controller_ids:
        item = snapshot.controller(controller_id)
        if item is None:
            raise TaskRejected("controller_not_found", f"控制器 {controller_id} 不存在", field="controller_ids")
        selected.append(item)
    return selected


def _foreign_present(snapshot: HostSnapshot, controller_index: int) -> bool:
    if snapshot.scan is None:
        return False
    topology = snapshot.scan.raid.topology(controller_index)
    if topology is None:
        return False
    for disk_id, state in (topology.disk_states or {}).items():
        text = str(state).lower()
        if "foreign" in text or "外部" in text:
            return True
    for disk_id, reason in (topology.blocked or {}).items():
        text = str(reason).lower()
        if "foreign" in text or "外部" in text:
            return True
    return False


def build_create(snapshot: HostSnapshot, params: Dict[str, Any]) -> CreatePlan:
    info, metadata, caps = _controller(snapshot, params["controller_id"])
    if not caps.get("create"):
        raise TaskRejected("unsupported_controller", "该控制器不支持建卷", detail={"capabilities": caps})
    disks = select_disks(snapshot, params["controller_id"], params["pds"])
    level = RaidLevel(params["raid_level"])
    levels = {str(item).upper().replace("RAID", "") for item in caps.get("levels") or []}
    if levels and level.number not in levels:
        raise TaskRejected("raid_level_unsupported", f"控制器不支持 {level.value}", field="raid_level")
    safety.check_create_targets(snapshot, metadata, disks, info.index)
    safety.check_controller_idle(snapshot, info.index)
    stripe_raw = params.get("strip_size_kb")
    layout = build_layout(
        level,
        disks,
        Raid0Mode.SINGLE,
        name=params["name"],
        stripe_kb=int(stripe_raw) if stripe_raw else None,
        name_max_len=15,
        init=params.get("init", "fast"),
    )
    try:
        ensure_valid(layout)
    except ValueError as exc:
        text = str(exc)
        code = "insufficient_disks" if "needs" in text or "even number" in text else "invalid_params"
        raise TaskRejected(code, text, field="pds") from None
    provider = provider_for(snapshot.scan.detection) if snapshot.scan else None
    if provider is None:
        raise TaskRejected("unsupported_controller", "没有可用的 RAID provider")
    plans = provider.create_plans(info, layout)
    if not plans:
        raise TaskRejected("unsupported_controller", "控制器未生成建卷命令")
    warnings = list(layout.warnings)
    if layout.name != params["name"]:
        warnings.append(f"VD 名称被规范化为 {layout.name!r}")
    if _foreign_present(snapshot, info.index):
        warnings.append("控制器存在外部配置，建卷前请确认")
    return CreatePlan(controller=info, layout=layout, plans=plans, warnings=warnings)


def build_delete(snapshot: HostSnapshot, params: Dict[str, Any]) -> DeletePlan:
    info, metadata, caps = _controller(snapshot, params["controller_id"])
    if not caps.get("delete_vd"):
        raise TaskRejected("unsupported_controller", "该控制器不支持删除 VD")
    safety.check_controller(metadata)
    volume = select_volume(
        snapshot,
        params["controller_id"],
        params["vd_id"],
        params["vd_name"],
        int(params["expected_size_bytes"]),
    )
    safety.check_boot_for_volumes(
        snapshot,
        info.index,
        [volume.volume_id],
        list(snapshot.scan.disks) if snapshot.scan else [],
    )
    safety.check_controller_idle(snapshot, info.index)
    provider = provider_for(snapshot.scan.detection) if snapshot.scan else None
    if provider is None:
        raise TaskRejected("unsupported_controller", "没有可用的 RAID provider")
    plans = provider.delete_plans(info, [volume])
    if not plans:
        raise TaskRejected("unsupported_controller", "控制器未生成删除命令")
    return DeletePlan(controller=info, volume=volume, plans=plans)


def build_clear(cfg: Config, snapshot: HostSnapshot, params: Dict[str, Any]) -> ClearPlan:
    info, metadata, caps = _controller(snapshot, params["controller_id"])
    if not caps.get("clear_config"):
        raise TaskRejected("unsupported_controller", "该控制器不支持清配置")
    if not getattr(cfg.raid, "allow_clear_config", False):
        raise TaskRejected("unsupported_controller", "本地配置禁止 clear_config")
    safety.check_controller(metadata)
    vds = snapshot.vds_for(params["controller_id"])
    if len(vds) != int(params["expected_vd_count"]):
        raise TaskRejected(
            "topology_changed",
            f"当前 VD 数 {len(vds)} 与 expected_vd_count={params['expected_vd_count']} 不一致",
            detail={"actual": len(vds), "expected": params["expected_vd_count"]},
        )
    foreign = _foreign_present(snapshot, info.index)
    if foreign and not params["include_foreign"]:
        raise TaskRejected(
            "foreign_config_present",
            "控制器存在外部配置；core 无法精确排除，已拒绝",
            field="include_foreign",
        )
    volume_ids = [str(item.get("vd_id")) for item in vds]
    safety.check_boot_for_volumes(
        snapshot,
        info.index,
        volume_ids,
        list(snapshot.scan.disks) if snapshot.scan else [],
    )
    safety.check_controller_idle(snapshot, info.index)
    provider = provider_for(snapshot.scan.detection) if snapshot.scan else None
    if provider is None:
        raise TaskRejected("unsupported_controller", "没有可用的 RAID provider")
    plans = provider.clear_plans(info)
    if not plans:
        raise TaskRejected("unsupported_controller", "控制器未生成清配置命令")
    warnings = ["包含外部配置清理"] if foreign and params["include_foreign"] else []
    return ClearPlan(controller=info, plans=plans, vd_count=len(vds), warnings=warnings)


def build_erase(cfg: Config, snapshot: HostSnapshot, params: Dict[str, Any]) -> ErasePlan:
    info, metadata, _caps = _controller(snapshot, params["controller_id"])
    disks = select_disks(snapshot, params["controller_id"], params["pds"])
    mode = params["mode"]
    for disk in disks:
        if mode not in available_methods(disk):
            raise TaskRejected(
                "unsupported_erase_mode",
                f"{mode} 不适用于 {disk.disk_id} ({disk.medium.value})",
                field="mode",
            )
    safety.check_erase_targets(snapshot, metadata, disks, bool(params["allow_raid_member"]))
    plans: List[CommandPlan] = []
    for disk in disks:
        try:
            plans.append(build_command(disk, mode, cfg))
        except ValueError as exc:
            raise TaskRejected("unsupported_erase_mode", str(exc), field="mode") from None
    per_disk = 0
    for disk in disks:
        per_disk += 600 if disk.medium.value == "HDD" else 300
    eta = per_disk * int(params["passes"])
    warnings: List[str] = []
    if params["passes"] > 1:
        warnings.append(f"将重复执行 {params['passes']} 轮擦除命令")
    return ErasePlan(
        controller=info,
        disks=disks,
        plans=plans,
        passes=int(params["passes"]),
        eta_seconds=eta,
        warnings=warnings,
    )


def build_read_result(snapshot: HostSnapshot, task: RemoteTask) -> Dict[str, Any]:
    params = task.params
    controllers = select_controllers(snapshot, params.get("controller_ids"))
    controller_ids = {item["controller_id"] for item in controllers}
    if task.type is RemoteTaskType.REFRESH_HARDWARE:
        return {
            "topology_hash": snapshot.topology_hash,
            "collected_at": snapshot.collected_at,
            "controller_count": len(snapshot.controllers),
            "pd_count": len(snapshot.pds),
            "vd_count": len(snapshot.vds),
            "boot_media_determined": snapshot.boot_media.determined,
            "storage": snapshot.storage,
        }
    if task.type is RemoteTaskType.LIST_CONTROLLERS:
        return {"controllers": controllers, "topology_hash": snapshot.topology_hash}
    if task.type is RemoteTaskType.LIST_PDS:
        pds = [item for item in snapshot.pds if item.get("controller_id") in controller_ids]
        return {
            "pds": [
                {
                    "controller_id": item.get("controller_id"),
                    "serial": item.get("serial"),
                    "wwn": item.get("wwn_normalized") or normalize_wwn(item.get("wwn")),
                    "expected_size_bytes": item.get("size_bytes"),
                    "media_type": str(item.get("medium", "")).lower(),
                    "state": item.get("raid_state"),
                    "raid_member_of": item.get("raid_member_of"),
                    "boot_protected": item.get("boot_protected"),
                }
                for item in pds
            ],
            "topology_hash": snapshot.topology_hash,
        }
    if task.type is RemoteTaskType.LIST_VDS:
        vds = [item for item in snapshot.vds if item.get("controller_id") in controller_ids]
        return {"vds": vds, "topology_hash": snapshot.topology_hash}
    raise TaskRejected("unsupported_task_type", "不是只读任务", field="type")


def verify_create(snapshot: HostSnapshot, controller_id: str, layout: RaidLayout) -> tuple[bool, Dict[str, Any]]:
    expected_members = {str(disk.serial) for disk in layout.disks}
    for volume in snapshot.vds_for(controller_id):
        if str(volume.get("vd_name")) != layout.name:
            continue
        level_ok = str(volume.get("raid_level", "")).upper().replace("RAID", "") == layout.level.number
        if int(layout.capacity_bytes) <= 0:
            size_ok = True
        else:
            size_ok = abs(int(volume.get("size_bytes", 0)) - int(layout.capacity_bytes)) <= max(
                1, int(layout.capacity_bytes) // 100
            )
        members_ok = set(volume.get("member_serials") or []) == expected_members
        detail = {
            "vd_id": volume.get("vd_id"),
            "level_ok": level_ok,
            "size_ok": size_ok,
            "members_ok": members_ok,
            "actual_size": volume.get("size_bytes"),
            "expected_size": layout.capacity_bytes,
        }
        return level_ok and size_ok and members_ok, detail
    return False, {"reason": "VD 未出现", "name": layout.name}


def verify_delete(snapshot: HostSnapshot, controller_id: str, vd_id: str) -> tuple[bool, Dict[str, Any]]:
    for volume in snapshot.vds_for(controller_id):
        if str(volume.get("vd_id")) == vd_id:
            return False, {"reason": "VD 仍存在", "vd_id": vd_id}
    return True, {"vd_id": vd_id}


def verify_clear(snapshot: HostSnapshot, controller_id: str, include_foreign: bool) -> tuple[bool, Dict[str, Any]]:
    vds = snapshot.vds_for(controller_id)
    ok = len(vds) == 0
    detail: Dict[str, Any] = {"remaining_vds": [item.get("vd_id") for item in vds]}
    if include_foreign:
        detail["foreign_present"] = any(
            "foreign" in str(item.get("state", "")).lower() for item in snapshot.pds
        )
        ok = ok and not detail["foreign_present"]
    return ok, detail


def verify_erase(snapshot: HostSnapshot, controller_id: str, plan: ErasePlan) -> tuple[bool, Dict[str, Any]]:
    current = {disk.disk_id: disk for disk in (snapshot.scan.disks if snapshot.scan else [])}
    missing: List[str] = []
    changed: List[str] = []
    for disk in plan.disks:
        other = current.get(disk.disk_id)
        if other is None:
            missing.append(disk.disk_id)
            continue
        if str(other.serial) != str(disk.serial) or normalize_wwn(other.wwn) != normalize_wwn(disk.wwn):
            changed.append(disk.disk_id)
    return not missing and not changed, {"missing": missing, "changed": changed}


__all__ = [
    "ClearPlan",
    "CreatePlan",
    "DeletePlan",
    "ErasePlan",
    "build_clear",
    "build_create",
    "build_delete",
    "build_erase",
    "build_read_result",
    "select_controllers",
    "select_disks",
    "select_volume",
    "verify_clear",
    "verify_create",
    "verify_delete",
    "verify_erase",
]
