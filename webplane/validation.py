"""Task parameter validation mirroring ``app/raidforge/agent/validation.py``.

The agent remains the final authority.  The control plane duplicates the rules
so the UI can reject obvious mistakes before a task is queued.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional

from .auth import ApiError

TASK_TYPES = {
    "refresh_hardware",
    "list_controllers",
    "list_pds",
    "list_vds",
    "create_raid",
    "delete_raid",
    "clear_config",
    "abort_current",
}
DESTRUCTIVE_TYPES = {"create_raid", "delete_raid", "clear_config"}
READ_TYPES = {"refresh_hardware", "list_controllers", "list_pds", "list_vds"}
RAID_LEVELS = {
    "0": "RAID0",
    "raid0": "RAID0",
    "1": "RAID1",
    "raid1": "RAID1",
    "5": "RAID5",
    "raid5": "RAID5",
    "10": "RAID10",
    "raid10": "RAID10",
}
_TASK_ID_RE = re.compile(r"^tsk_[A-Za-z0-9]{8,64}$")


class TaskValidationError(ApiError):
    status = 400
    code = "validation_failed"

    def __init__(self, message: str, *, code: str = "invalid_params", field: str = "") -> None:
        fields = [{"field": field, "message": message}] if field else []
        super().__init__(message, code=code, status=400, fields=fields)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _require_dict(value: Any, field: str) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise TaskValidationError(f"{field} 必须是对象", field=field)
    return value


def _reject_extra(payload: Dict[str, Any], allowed: Iterable[str], prefix: str = "") -> None:
    for key in payload:
        if key not in allowed:
            name = f"{prefix}{key}" if prefix else str(key)
            raise TaskValidationError(f"未知字段 {name}", field=name)


def _optional_bool(payload: Dict[str, Any], key: str, default: bool) -> bool:
    value = payload.get(key, default)
    if not isinstance(value, bool):
        raise TaskValidationError(f"{key} 必须是 bool", field=key)
    return value


def _optional_int(
    payload: Dict[str, Any], key: str, default: int, *, minimum: int, maximum: int
) -> int:
    value = payload.get(key, default)
    if not _is_int(value):
        raise TaskValidationError(f"{key} 必须是整数", field=key)
    if value < minimum or value > maximum:
        raise TaskValidationError(f"{key} 超出范围 {minimum}..{maximum}", field=key)
    return value


def _optional_stripe(payload: Dict[str, Any], key: str) -> Optional[int]:
    value = payload.get(key)
    if value in (None, "", 0):
        return None
    if not _is_int(value):
        raise TaskValidationError(f"{key} 必须是整数", field=key)
    if value < 16 or value > 4096:
        raise TaskValidationError(
            f"{key} 超出范围 16..4096（留空或 0 表示控制器默认）", field=key
        )
    return int(value)


def _require_str(payload: Dict[str, Any], key: str, *, maximum: int = 256) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise TaskValidationError(f"{key} 非法", field=key)
    return value


def _optional_str(
    payload: Dict[str, Any], key: str, default: str = "", *, maximum: int = 256
) -> str:
    value = payload.get(key, default)
    if value is None:
        return default
    if not isinstance(value, str) or len(value) > maximum:
        raise TaskValidationError(f"{key} 非法", field=key)
    return value


def _selectors(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    raw = payload.get("pds")
    if not isinstance(raw, list) or not raw:
        raise TaskValidationError("pds 必须是非空数组", field="pds")
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
            raise TaskValidationError(
                "expected_size_bytes 必须是正整数", field=prefix + "expected_size_bytes"
            )
        if not serial or not wwn:
            raise TaskValidationError(
                "破坏性任务要求 serial 与 wwn 同时非空",
                code="disk_identity_insufficient",
                field=prefix + ("wwn" if not wwn else "serial"),
            )
        if not re.match(r"^0x[0-9a-fA-F]{16}$", wwn):
            raise TaskValidationError("wwn 必须为 0x + 16 位十六进制", field=prefix + "wwn")
        media = entry.get("media_type", "")
        if media and media not in {"hdd", "ssd", "HDD", "SSD"}:
            raise TaskValidationError("media_type 非法", field=prefix + "media_type")
        key = (serial, wwn.lower(), size)
        if key in seen:
            raise TaskValidationError("pds 中存在重复目标", field="pds")
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


def validate_task_params(task_type: str, params: Any) -> Dict[str, Any]:
    if task_type not in TASK_TYPES:
        raise TaskValidationError(f"未知任务类型 {task_type!r}", code="unsupported_task_type", field="type")
    payload = _require_dict(params or {}, "params")
    if task_type in READ_TYPES:
        return _read_params(payload)
    if task_type == "abort_current":
        return _abort_params(payload)
    if task_type == "create_raid":
        return _create_params(payload)
    if task_type == "delete_raid":
        return _delete_params(payload)
    if task_type == "clear_config":
        return _clear_params(payload)
    raise TaskValidationError("未知任务类型", code="unsupported_task_type", field="type")


def _read_params(params: Dict[str, Any]) -> Dict[str, Any]:
    _reject_extra(params, {"controller_ids", "include_raw_output"})
    controllers = params.get("controller_ids")
    if controllers is not None:
        if not isinstance(controllers, list) or any(
            not isinstance(item, str) or not item for item in controllers
        ):
            raise TaskValidationError("controller_ids 必须是字符串数组", field="controller_ids")
    return {
        "controller_ids": list(controllers) if controllers is not None else None,
        "include_raw_output": _optional_bool(params, "include_raw_output", False),
    }


def _abort_params(params: Dict[str, Any]) -> Dict[str, Any]:
    _reject_extra(params, {"reason", "target_task_id"})
    reason = _optional_str(params, "reason", "operator requested", maximum=200)
    target = _optional_str(params, "target_task_id", "", maximum=64)
    if target and not _TASK_ID_RE.match(target):
        raise TaskValidationError("target_task_id 非法", field="target_task_id")
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
    if level_raw not in RAID_LEVELS:
        raise TaskValidationError(
            f"不支持的 RAID 级别 {level_raw!r}", code="raid_level_unsupported", field="raid_level"
        )
    level = RAID_LEVELS[level_raw]
    selectors = _selectors(params)
    stripe = _optional_stripe(params, "strip_size_kb")

    read_policy = _optional_str(params, "read_policy", "no_read_ahead", maximum=32)
    if read_policy not in ("no_read_ahead", ""):
        raise TaskValidationError("read_policy 未实现", field="read_policy")
    write_policy = _optional_str(params, "write_policy", "write_back", maximum=32)
    if write_policy not in ("write_back", ""):
        raise TaskValidationError("write_policy 未实现", field="write_policy")
    init = _optional_str(params, "init", "fast", maximum=32).strip().lower()
    if init in ("", "fast"):
        init = "fast"
    elif init == "none":
        init = "none"
    else:
        raise TaskValidationError("init 仅支持 fast/none", field="init")
    size_bytes = params.get("size_bytes")
    if size_bytes not in (None, 0):
        raise TaskValidationError("size_bytes 未实现", field="size_bytes")
    if _optional_bool(params, "bootable", False):
        raise TaskValidationError("bootable 未实现", field="bootable")
    if _optional_bool(params, "force", False):
        raise TaskValidationError("force 未实现", field="force")
    return {
        "controller_id": controller_id,
        "name": name,
        "raid_level": level,
        "pds": selectors,
        "strip_size_kb": stripe,
        "init": init,
        "dry_run": _optional_bool(params, "dry_run", False),
    }


def _delete_params(params: Dict[str, Any]) -> Dict[str, Any]:
    _reject_extra(params, {"controller_id", "vd_id", "vd_name", "expected_size_bytes", "dry_run"})
    controller_id = _require_str(params, "controller_id", maximum=128)
    vd_id = _require_str(params, "vd_id", maximum=64)
    vd_name = _require_str(params, "vd_name", maximum=64)
    size = params.get("expected_size_bytes")
    if not _is_int(size) or size <= 0:
        raise TaskValidationError("expected_size_bytes 必须是正整数", field="expected_size_bytes")
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
        raise TaskValidationError("expected_vd_count 必须是非负整数", field="expected_vd_count")
    return {
        "controller_id": controller_id,
        "include_foreign": _optional_bool(params, "include_foreign", False),
        "expected_vd_count": int(count),
        "dry_run": _optional_bool(params, "dry_run", False),
    }


__all__ = [
    "DESTRUCTIVE_TYPES",
    "RAID_LEVELS",
    "READ_TYPES",
    "TASK_TYPES",
    "TaskValidationError",
    "validate_task_params",
]
