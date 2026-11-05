"""Task metadata and form templates served to the Vue UI."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict, List

from .validation import DESTRUCTIVE_TYPES

_READ_FIELDS: List[Dict[str, Any]] = [
    {
        "name": "controller_ids",
        "label": "控制器 ID",
        "type": "string-list",
        "required": False,
        "help": "留空表示全部控制器",
    },
    {
        "name": "include_raw_output",
        "label": "包含原始输出",
        "type": "boolean",
        "default": False,
    },
]

_CONTROLLER_FIELD = {
    "name": "controller_id",
    "label": "控制器",
    "type": "controller",
    "required": True,
    "help": "来自 agent 心跳上报的 capabilities.controllers",
}
_SELECTOR_FIELD = {
    "name": "pds",
    "label": "目标磁盘",
    "type": "selectors",
    "required": True,
    "help": "serial 与 wwn 必须同时填写；wwn 格式 0x + 16 位十六进制",
}
_DRY_RUN_FIELD = {
    "name": "dry_run",
    "label": "仅预演（dry_run）",
    "type": "boolean",
    "default": True,
    "help": "关闭后才会真正写入控制器",
}

_TASK_TYPES: List[Dict[str, Any]] = [
    {
        "type": "refresh_hardware",
        "label": "刷新硬件",
        "description": "重新扫描控制器、物理盘与 RAID 拓扑。",
        "template": {"controller_ids": None, "include_raw_output": False},
        "fields": _READ_FIELDS,
    },
    {
        "type": "list_controllers",
        "label": "列出控制器",
        "description": "读取阵列卡列表与工具版本。",
        "template": {"controller_ids": None, "include_raw_output": False},
        "fields": _READ_FIELDS,
    },
    {
        "type": "list_pds",
        "label": "列出物理盘",
        "description": "读取物理盘清单与状态。",
        "template": {"controller_ids": None, "include_raw_output": False},
        "fields": _READ_FIELDS,
    },
    {
        "type": "list_vds",
        "label": "列出虚拟盘",
        "description": "读取 RAID 卷与成员盘。",
        "template": {"controller_ids": None, "include_raw_output": False},
        "fields": _READ_FIELDS,
    },
    {
        "type": "create_raid",
        "label": "创建 RAID",
        "description": "创建 RAID0/1/5/10 虚拟盘。",
        "template": {
            "controller_id": "",
            "name": "",
            "raid_level": "RAID0",
            "pds": [],
            "strip_size_kb": None,
            "init": "fast",
            "dry_run": True,
        },
        "fields": [
            _CONTROLLER_FIELD,
            {"name": "name", "label": "卷名称", "type": "text", "required": True},
            {
                "name": "raid_level",
                "label": "RAID 级别",
                "type": "select",
                "options": ["RAID0", "RAID1", "RAID5", "RAID10"],
                "default": "RAID0",
                "required": True,
            },
            _SELECTOR_FIELD,
            {
                "name": "strip_size_kb",
                "label": "条带大小 (KB)",
                "type": "number",
                "default": None,
                "min": 16,
                "max": 4096,
                "help": "留空使用控制器默认条带；填写 16-4096 才显式下发。",
            },
            {
                "name": "init",
                "label": "初始化方式",
                "type": "select",
                "options": [
                    {"value": "fast", "label": "快速初始化 (fast)"},
                    {"value": "none", "label": "不显式指定 (none)"},
                ],
                "default": "fast",
                "help": "fast 为默认；LSI 始终使用控制器默认快速初始化。",
            },
            _DRY_RUN_FIELD,
        ],
    },
    {
        "type": "delete_raid",
        "label": "删除 RAID",
        "description": "删除指定虚拟盘。",
        "template": {
            "controller_id": "",
            "vd_id": "",
            "vd_name": "",
            "expected_size_bytes": 0,
            "dry_run": True,
        },
        "fields": [
            _CONTROLLER_FIELD,
            {"name": "vd_id", "label": "VD ID", "type": "text", "required": True},
            {"name": "vd_name", "label": "VD 名称", "type": "text", "required": True},
            {
                "name": "expected_size_bytes",
                "label": "预期容量 (bytes)",
                "type": "number",
                "min": 1,
                "required": True,
            },
            _DRY_RUN_FIELD,
        ],
    },
    {
        "type": "clear_config",
        "label": "清除控制器配置",
        "description": "删除所有虚拟盘并清理 foreign/残留元数据。",
        "template": {
            "controller_id": "",
            "expected_vd_count": 0,
            "include_foreign": False,
            "dry_run": True,
        },
        "fields": [
            _CONTROLLER_FIELD,
            {
                "name": "expected_vd_count",
                "label": "预期 VD 数量",
                "type": "number",
                "min": 0,
                "default": 0,
                "required": True,
            },
            {
                "name": "include_foreign",
                "label": "包含 Foreign 配置",
                "type": "boolean",
                "default": False,
            },
            _DRY_RUN_FIELD,
        ],
    },
    {
        "type": "erase_disks",
        "label": "擦除磁盘",
        "description": "向控制器提交物理盘擦除命令（异步）。",
        "template": {
            "controller_id": "",
            "mode": "thorough",
            "passes": 1,
            "verify": False,
            "allow_raid_member": False,
            "pds": [],
            "dry_run": True,
        },
        "fields": [
            _CONTROLLER_FIELD,
            {
                "name": "mode",
                "label": "擦除模式",
                "type": "select",
                "options": ["thorough", "overwrite", "erase", "blockerase", "sanitize"],
                "default": "thorough",
                "required": True,
            },
            {
                "name": "passes",
                "label": "遍数",
                "type": "number",
                "default": 1,
                "min": 1,
                "max": 3,
            },
            {
                "name": "verify",
                "label": "执行校验（必须关闭）",
                "type": "boolean",
                "default": False,
                "help": "agent 当前未实现 verify=true，保持关闭",
            },
            {
                "name": "allow_raid_member",
                "label": "允许 RAID 成员盘",
                "type": "boolean",
                "default": False,
            },
            _SELECTOR_FIELD,
            _DRY_RUN_FIELD,
        ],
    },
    {
        "type": "abort_current",
        "label": "中止当前任务",
        "description": "请求 agent 在当前命令边界中止执行。",
        "template": {"reason": "operator requested", "target_task_id": ""},
        "fields": [
            {"name": "reason", "label": "原因", "type": "text", "default": "operator requested"},
            {
                "name": "target_task_id",
                "label": "目标任务 ID",
                "type": "text",
                "help": "留空表示 agent 当前任务",
            },
        ],
    },
]

for item in _TASK_TYPES:
    item["destructive"] = item["type"] in DESTRUCTIVE_TYPES
    item["confirm_required"] = item["destructive"]


def get_task_types() -> List[Dict[str, Any]]:
    return deepcopy(_TASK_TYPES)


def get_task_type(task_type: str) -> Dict[str, Any] | None:
    for item in _TASK_TYPES:
        if item["type"] == task_type:
            return deepcopy(item)
    return None


__all__ = ["get_task_types", "get_task_type"]
