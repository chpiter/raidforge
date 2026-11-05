"""任务信封与参数严格校验。

未知字段一律 ``invalid_params``；任何破坏性目标都要求 serial 与 wwn
同时非空，并禁止以索引/槽位/设备节点作为唯一标识。
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional

from .clock import Clock, parse_rfc3339
from .models import RemoteTask, RemoteTaskType, SCHEMA_VERSION, TaskRejected

_TASK_ID_RE = re.compile(r"^tsk_[A-Za-z0-9]{8,64}$")
_LEASE_ID_RE = re.compile(r"^lse_[A-Za-z0-9]{8,64}$")
_RAID_LEVELS = {
    "0": "RAID0",
    "raid0": "RAID0",
    "1": "RAID1",
    "raid1": "RAID1",
    "5": "RAID5",
    "raid5": "RAID5",
    "10": "RAID10",
    "raid10": "RAID10",
}
_READ_TYPES = {
    RemoteTaskType.REFRESH_HARDWARE,
    RemoteTaskType.LIST_CONTROLLERS,
    RemoteTaskType.LIST_PDS,
    RemoteTaskType.LIST_VDS,
}


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _require_dict(value: Any, field: str) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise TaskRejected("invalid_params", f"{field} 必须是对象", field=field)
    return value


def _reject_extra(payload: Dict[str, Any], allowed: Iterable[str], prefix: str = "") -> None:
    for key in payload:
        if key not in allowed:
            name = f"{prefix}{key}" if prefix else str(key)
            raise TaskRejected("invalid_params", f"未知字段 {name}", field=name)


def _optional_bool(payload: Dict[str, Any], key: str, default: bool) -> bool:
    value = payload.get(key, default)
    if not isinstance(value, bool):
        raise TaskRejected("invalid_params", f"{key} 必须是 bool", field=key)
    return value


def _optional_int(payload: Dict[str, Any], key: str, default: int, *, minimum: int, maximum: int) -> int:
    value = payload.get(key, default)
    if not _is_int(value):
        raise TaskRejected("invalid_params", f"{key} 必须是整数", field=key)
    if value < minimum or value > maximum:
        raise TaskRejected(
            "invalid_params", f"{key} 超出范围 {minimum}..{maximum}", field=key
        )
    return value


def _optional_stripe(payload: Dict[str, Any], key: str) -> Optional[int]:
    value = payload.get(key)
    if value in (None, "", 0):
        return None
    if not _is_int(value):
        raise TaskRejected("invalid_params", f"{key} 必须是整数", field=key)
    if value < 16 or value > 4096:
        raise TaskRejected(
            "invalid_params",
            f"{key} 超出范围 16..4096（留空或 0 表示控制器默认）",
            field=key,
        )
    return int(value)


def _require_str(payload: Dict[str, Any], key: str, *, maximum: int = 256) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise TaskRejected("invalid_params", f"{key} 非法", field=key)
    return value


def _optional_str(payload: Dict[str, Any], key: str, default: str = "", *, maximum: int = 256) -> str:
    value = payload.get(key, default)
    if value is None:
        return default
    if not isinstance(value, str) or len(value) > maximum:
        raise TaskRejected("invalid_params", f"{key} 非法", field=key)
    return value


def _selectors(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    raw = payload.get("pds")
    if not isinstance(raw, list) or not raw:
        raise TaskRejected("invalid_params", "pds 必须是非空数组", field="pds")
    selectors: List[Dict[str, Any]] = []
    seen = set()
    for index, item in enumerate(raw):
        prefix = f"pds[{index}]."
        entry = _require_dict(item, f"pds[{index}]")
        _reject_extra(entry, {"serial", "wwn", "expected_size_bytes", "media_type"}, prefix)
        serial = _optional_str(entry, "serial", "", maximum=128)
        wwn = _optional_str(entry, "wwn", "", maximum=64)
        size = entry.get("expected_size_bytes")
        if not _is_int(size) or size <= 0:
            raise TaskRejected("invalid_params", "expected_size_bytes 必须是正整数", field=prefix + "expected_size_bytes")
        if not serial and not wwn:
            raise TaskRejected("invalid_params", "serial 与 wwn 至少一个非空", field=prefix + "serial")
        if not serial or not wwn:
            raise TaskRejected(
                "disk_identity_insufficient",
                "破坏性任务要求 serial 与 wwn 同时非空",
                field=prefix + "wwn" if not wwn else prefix + "serial",
            )
        if not wwn.lower().startswith("0x"):
            raise TaskRejected("invalid_params", "wwn 必须为 0x + 16 位十六进制", field=prefix + "wwn")
        if not re.match(r"^0x[0-9a-fA-F]{16}$", wwn):
            raise TaskRejected("invalid_params", "wwn 格式非法", field=prefix + "wwn")
        media = entry.get("media_type", "")
        if media and media not in {"hdd", "ssd", "HDD", "SSD"}:
            raise TaskRejected("invalid_params", "media_type 非法", field=prefix + "media_type")
        key = (serial, wwn.lower(), size)
        if key in seen:
            raise TaskRejected("invalid_params", "pds 中存在重复目标", field="pds")
        seen.add(key)
        selectors.append(
            {
                "serial": serial,
                "wwn": wwn,
                "expected_size_bytes": int(size),
                "media_type": media.lower() if media else "",
            }
        )
    return selectors


def parse_task(envelope: Dict[str, Any]) -> RemoteTask:
    if not isinstance(envelope, dict):
        raise TaskRejected("invalid_params", "task 必须是对象")
    schema = envelope.get("schema_version", SCHEMA_VERSION)
    if schema != SCHEMA_VERSION:
        raise TaskRejected("invalid_params", f"schema_version={schema} 不支持", field="schema_version")

    task_id = envelope.get("task_id")
    if not isinstance(task_id, str) or not _TASK_ID_RE.match(task_id):
        raise TaskRejected("invalid_params", "task_id 非法", field="task_id")
    lease_id = envelope.get("lease_id")
    if not isinstance(lease_id, str) or not _LEASE_ID_RE.match(lease_id):
        raise TaskRejected("invalid_params", "lease_id 非法", field="lease_id")

    raw_type = envelope.get("type")
    try:
        task_type = RemoteTaskType(str(raw_type))
    except ValueError:
        raise TaskRejected(
            "unsupported_task_type", f"未知任务类型 {raw_type!r}", field="type"
        ) from None

    if task_type is RemoteTaskType.ERASE:
        raise TaskRejected(
            "remote_erase_disabled",
            "远程擦除已禁用，请在本地 TUI 执行",
            field="type",
        )

    server_sn = envelope.get("server_sn")
    if not isinstance(server_sn, str) or not server_sn.strip() or len(server_sn) > 128:
        raise TaskRejected("invalid_params", "server_sn 非法", field="server_sn")

    params = envelope.get("params") or {}
    if not isinstance(params, dict):
        raise TaskRejected("invalid_params", "params 必须是对象", field="params")

    timeout = envelope.get("timeout_seconds", 600)
    if not _is_int(timeout) or timeout <= 0:
        raise TaskRejected("invalid_params", "timeout_seconds 必须是正整数", field="timeout_seconds")

    require_confirm = envelope.get("require_local_confirm", True)
    if not isinstance(require_confirm, bool):
        raise TaskRejected("invalid_params", "require_local_confirm 必须是 bool", field="require_local_confirm")

    task = RemoteTask(
        task_id=task_id,
        type=task_type,
        server_sn=server_sn.strip(),
        params=dict(params),
        require_local_confirm=require_confirm,
        timeout_seconds=int(timeout),
        lease_id=lease_id,
        lease_expires_at=str(envelope.get("lease_expires_at") or ""),
        created_by=str(envelope.get("created_by") or ""),
        created_at=str(envelope.get("created_at") or ""),
        expires_at=str(envelope.get("expires_at") or ""),
        priority=int(envelope.get("priority") or 100),
        schema_version=SCHEMA_VERSION,
        raw=dict(envelope),
    )
    task.params = validate_params(task)
    return task


def validate_params(task: RemoteTask) -> Dict[str, Any]:
    if task.type in _READ_TYPES:
        return _read_params(task.params)
    if task.type is RemoteTaskType.ABORT_CURRENT:
        return _abort_params(task.params)
    if task.type is RemoteTaskType.CREATE_RAID:
        return _create_params(task.params)
    if task.type is RemoteTaskType.DELETE_RAID:
        return _delete_params(task.params)
    if task.type is RemoteTaskType.CLEAR_CONFIG:
        return _clear_params(task.params)
    raise TaskRejected("unsupported_task_type", "未知任务类型", field="type")


def _read_params(params: Dict[str, Any]) -> Dict[str, Any]:
    _reject_extra(params, {"controller_ids", "include_raw_output"})
    controllers = params.get("controller_ids")
    if controllers is not None:
        if not isinstance(controllers, list) or any(
            not isinstance(item, str) or not item for item in controllers
        ):
            raise TaskRejected("invalid_params", "controller_ids 必须是字符串数组", field="controller_ids")
    include_raw = _optional_bool(params, "include_raw_output", False)
    return {"controller_ids": list(controllers) if controllers is not None else None, "include_raw_output": include_raw}


def _abort_params(params: Dict[str, Any]) -> Dict[str, Any]:
    _reject_extra(params, {"reason", "target_task_id"})
    reason = _optional_str(params, "reason", "operator requested", maximum=200)
    target = _optional_str(params, "target_task_id", "", maximum=64)
    if target and not _TASK_ID_RE.match(target):
        raise TaskRejected("invalid_params", "target_task_id 非法", field="target_task_id")
    return {"reason": reason, "target_task_id": target}


def _create_params(params: Dict[str, Any]) -> Dict[str, Any]:
    allowed = {
        "controller_id",
        "name",
        "raid_level",
        "pds",
        "strip_size_kb",
        "read_policy",
        "write_policy",
        "init",
        "size_bytes",
        "bootable",
        "force",
        "dry_run",
    }
    _reject_extra(params, allowed)
    controller_id = _require_str(params, "controller_id", maximum=128)
    name = _require_str(params, "name", maximum=64)
    level_raw = str(params.get("raid_level") or "").strip().lower()
    if level_raw not in _RAID_LEVELS:
        raise TaskRejected("raid_level_unsupported", f"不支持的 RAID 级别 {level_raw!r}", field="raid_level")
    level = _RAID_LEVELS[level_raw]
    selectors = _selectors(params)
    stripe = _optional_stripe(params, "strip_size_kb")

    # core 只支持默认策略；非默认值明确拒绝，而不是静默忽略。
    read_policy = _optional_str(params, "read_policy", "no_read_ahead", maximum=32)
    if read_policy not in ("no_read_ahead", ""):
        raise TaskRejected("invalid_params", "read_policy 未实现", field="read_policy")
    write_policy = _optional_str(params, "write_policy", "write_back", maximum=32)
    if write_policy not in ("write_back", ""):
        raise TaskRejected("invalid_params", "write_policy 未实现", field="write_policy")
    init = _optional_str(params, "init", "fast", maximum=32).strip().lower()
    if init in ("", "fast"):
        init = "fast"
    elif init == "none":
        init = "none"
    else:
        raise TaskRejected("invalid_params", "init 仅支持 fast/none", field="init")
    size_bytes = params.get("size_bytes")
    if size_bytes not in (None, 0):
        raise TaskRejected("invalid_params", "size_bytes 未实现", field="size_bytes")
    bootable = _optional_bool(params, "bootable", False)
    if bootable:
        raise TaskRejected("invalid_params", "bootable 未实现", field="bootable")
    force = _optional_bool(params, "force", False)
    if force:
        raise TaskRejected("invalid_params", "force 未实现", field="force")
    dry_run = _optional_bool(params, "dry_run", False)
    return {
        "controller_id": controller_id,
        "name": name,
        "raid_level": level,
        "pds": selectors,
        "strip_size_kb": stripe,
        "init": init,
        "dry_run": dry_run,
    }


def _delete_params(params: Dict[str, Any]) -> Dict[str, Any]:
    _reject_extra(params, {"controller_id", "vd_id", "vd_name", "expected_size_bytes", "dry_run"})
    controller_id = _require_str(params, "controller_id", maximum=128)
    vd_id = _require_str(params, "vd_id", maximum=64)
    vd_name = _require_str(params, "vd_name", maximum=64)
    size = params.get("expected_size_bytes")
    if not _is_int(size) or size <= 0:
        raise TaskRejected("invalid_params", "expected_size_bytes 必须是正整数", field="expected_size_bytes")
    return {
        "controller_id": controller_id,
        "vd_id": vd_id,
        "vd_name": vd_name,
        "expected_size_bytes": int(size),
        "dry_run": _optional_bool(params, "dry_run", False),
    }


def _clear_params(params: Dict[str, Any]) -> Dict[str, Any]:
    _reject_extra(params, {"controller_id", "include_foreign", "expected_vd_count", "dry_run"})
    controller_id = _require_str(params, "controller_id", maximum=128)
    count = params.get("expected_vd_count")
    if not _is_int(count) or count < 0:
        raise TaskRejected("invalid_params", "expected_vd_count 必须是非负整数", field="expected_vd_count")
    return {
        "controller_id": controller_id,
        "include_foreign": _optional_bool(params, "include_foreign", False),
        "expected_vd_count": int(count),
        "dry_run": _optional_bool(params, "dry_run", False),
    }


def validate_envelope(task: RemoteTask, server_sn: str, now: datetime) -> None:
    if task.server_sn != server_sn:
        raise TaskRejected(
            "server_sn_mismatch",
            f"任务 server_sn={task.server_sn!r} 与本地 {server_sn!r} 不一致",
            detail={"expected": server_sn, "actual": task.server_sn},
        )
    expires = parse_rfc3339(task.expires_at) if task.expires_at else None
    if expires is not None and now > expires:
        raise TaskRejected("task_expired", f"任务已于 {task.expires_at} 过期")
    lease_expires = parse_rfc3339(task.lease_expires_at) if task.lease_expires_at else None
    if lease_expires is not None and now > lease_expires:
        raise TaskRejected("lease_expired", f"租约已于 {task.lease_expires_at} 过期")


__all__ = ["parse_task", "validate_envelope", "validate_params"]
