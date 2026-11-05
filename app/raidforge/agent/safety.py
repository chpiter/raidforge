"""破坏性操作安全闸门（仅远程任务）。"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence

from ..core.models import DiskInfo
from .bootmedia import BootMediaMap
from .clock import rfc3339, parse_rfc3339
from .models import RemoteTask, TaskRejected
from .snapshot import HostSnapshot
from .topology import normalize_wwn, topology_diff


@dataclass
class Authorization:
    task_id: str
    lease_id: str
    topology_hash: str
    operator: str
    confirmed_at: str = field(default_factory=rfc3339)
    abort: threading.Event = field(default_factory=threading.Event, repr=False)


def check_controller(controller: Dict[str, Any]) -> None:
    if not controller:
        raise TaskRejected("controller_not_found", "控制器不存在")
    if not controller.get("serial"):
        raise TaskRejected(
            "unsupported_controller",
            "控制器缺少序列号，不能作为破坏性目标",
            detail={"controller_id": controller.get("controller_id", "")},
        )


def check_boot_media(snapshot: HostSnapshot, disks: Sequence[DiskInfo]) -> None:
    boot: BootMediaMap = snapshot.boot_media
    if not boot.determined:
        raise TaskRejected(
            "boot_media_undetermined",
            "无法判定启动介质身份，禁止一切写操作",
            detail={"error": boot.error},
        )
    for disk in disks:
        if boot.is_protected(disk.serial, disk.wwn):
            hit = boot.disk_for(disk.serial, disk.wwn)
            raise TaskRejected(
                "boot_media_protected",
                f"目标盘 {disk.disk_id} 是启动/Live/挂载介质",
                detail={"disk": disk.to_json_obj(), "protected": hit.to_dict() if hit else {}},
            )
        if not disk.serial or not normalize_wwn(disk.wwn):
            raise TaskRejected(
                "disk_identity_insufficient",
                f"目标盘 {disk.disk_id} 缺少 serial 或可解析 wwn",
                field="pds",
            )


def check_pd_removable(snapshot: HostSnapshot, disk: DiskInfo) -> None:
    protected = snapshot.boot_media.disk_for(disk.serial, disk.wwn)
    if protected is not None and protected.removable:
        raise TaskRejected(
            "disk_removable",
            f"目标盘 {disk.disk_id} 是可移动盘",
            detail={"disk": disk.disk_id},
        )


def _pd_payload(snapshot: HostSnapshot, disk: DiskInfo) -> Dict[str, Any]:
    for item in snapshot.pds:
        if item.get("disk_id") == disk.disk_id:
            return item
    return {}


def _blocked_code(reason: str) -> str:
    text = (reason or "").lower()
    if "boot" in text or "live" in text:
        return "boot_media_protected"
    if "remov" in text:
        return "disk_removable"
    if "foreign" in text or "外部" in text:
        return "foreign_config_present"
    return "disk_in_use"


def check_create_targets(
    snapshot: HostSnapshot,
    controller: Dict[str, Any],
    disks: Sequence[DiskInfo],
    controller_index: int,
) -> None:
    check_controller(controller)
    check_boot_media(snapshot, disks)

    topology = snapshot.scan.raid.topology(controller_index) if snapshot.scan else None
    free_ids = set(topology.free_disk_ids) if topology is not None else set()
    blocked = dict(topology.blocked) if topology is not None else {}
    states = dict(topology.disk_states) if topology is not None else {}

    for disk in disks:
        payload = _pd_payload(snapshot, disk)
        state = str(payload.get("raid_state") or states.get(disk.disk_id, "")).lower()
        if "foreign" in state or "外部" in state:
            raise TaskRejected("foreign_config_present", f"{disk.disk_id} 带外部配置", field="pds")
        if payload.get("raid_member_of"):
            raise TaskRejected("disk_in_use", f"{disk.disk_id} 已被 RAID 占用", field="pds")
        if disk.disk_id in blocked and disk.disk_id not in free_ids:
            raise TaskRejected(
                _blocked_code(str(blocked.get(disk.disk_id))),
                f"{disk.disk_id} 不可用于建卷: {blocked.get(disk.disk_id)}",
                field="pds",
            )
        if free_ids and disk.disk_id not in free_ids:
            raise TaskRejected("disk_in_use", f"{disk.disk_id} 不是 Unconfigured Good", field="pds")
        if "rebuild" in state or "initial" in state or "migrat" in state:
            raise TaskRejected("controller_busy", f"{disk.disk_id} 正在后台任务中", field="pds")


def check_controller_idle(snapshot: HostSnapshot, controller_index: int) -> None:
    if snapshot.scan is None:
        return
    topology = snapshot.scan.raid.topology(controller_index)
    if topology is None:
        return
    for disk_id, state in (topology.disk_states or {}).items():
        text = str(state).lower()
        if "rebuild" in text or "initial" in text or "migrat" in text:
            raise TaskRejected(
                "controller_busy",
                f"控制器 c{controller_index} 后台任务进行中",
                detail={"disk": disk_id, "state": state},
            )


def check_boot_for_volumes(
    snapshot: HostSnapshot,
    controller_index: int,
    volume_ids: Iterable[str],
    disks: Sequence[DiskInfo],
) -> None:
    if not snapshot.boot_media.determined:
        raise TaskRejected(
            "boot_media_undetermined",
            "无法判定启动介质身份，禁止一切写操作",
            detail={"error": snapshot.boot_media.error},
        )
    disk_map = {disk.disk_id: disk for disk in disks}
    if snapshot.scan is None:
        return
    topology = snapshot.scan.raid.topology(controller_index)
    if topology is None:
        return
    wanted = set(volume_ids)
    for volume in topology.volumes:
        if volume.volume_id not in wanted:
            continue
        for disk_id in volume.disk_ids:
            disk = disk_map.get(disk_id)
            if disk is not None and snapshot.boot_media.is_protected(disk.serial, disk.wwn):
                raise TaskRejected(
                    "boot_media_protected",
                    f"VD {volume.volume_id} 的成员盘 {disk_id} 属于启动介质",
                )


def check_topology(
    before_hash: str,
    fresh: HostSnapshot,
    before_projection: Optional[Dict[str, Any]] = None,
) -> None:
    if not before_hash or before_hash == fresh.topology_hash:
        return
    diff: Dict[str, Any] = {}
    if before_projection and fresh.projection:
        diff = topology_diff(before_projection, fresh.projection)
    raise TaskRejected(
        "topology_changed",
        "确认后拓扑发生变化，已中止",
        detail={"before": before_hash, "after": fresh.topology_hash, "diff": diff},
    )


def check_lease(task: RemoteTask, now: datetime) -> None:
    expires = parse_rfc3339(task.lease_expires_at) if task.lease_expires_at else None
    if expires is not None and now > expires:
        raise TaskRejected("lease_expired", "租约已过期")


def confirm_record(
    task: RemoteTask,
    decision: Any,
    *,
    policy_override_ignored: bool,
    method: str = "tui_yes",
) -> Dict[str, Any]:
    return {
        "required": True,
        "confirmed": bool(decision.approved),
        "method": method if decision.approved else "tui_rejected",
        "by": decision.operator,
        "at": decision.at,
        "policy_override_ignored": bool(policy_override_ignored),
    }


__all__ = [
    "Authorization",
    "check_boot_for_volumes",
    "check_boot_media",
    "check_controller",
    "check_controller_idle",
    "check_create_targets",
    "check_lease",
    "check_pd_removable",
    "check_topology",
    "confirm_record",
    "rfc3339",
]
