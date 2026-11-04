"""拓扑规范化与哈希。

哈希只覆盖稳定字段，排除时间戳、设备路径、槽位等易变信息，避免同一硬件
两次扫描产生不同哈希。
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Dict, Iterable, List, Mapping, Sequence

_HEX_RE = re.compile(r"^[0-9a-f]{8,32}$")


def normalize_wwn(raw: Any) -> str:
    """把各厂商/内核的 WWN 统一为 ``0x`` + 小写十六进制。

    支持 ``5000C500A4A2B001``、``0x5000c500a4a2b001``、``5 00a075 1161abcd1``。
    无法确定为 16 位十六进制时返回空串（调用方按身份不足处理）。
    """

    text = str(raw or "").strip().lower()
    if not text:
        return ""
    if text.startswith("0x"):
        text = text[2:]
    text = re.sub(r"\s+", "", text)
    if text.startswith("naa."):
        text = text[4:]
    if not _HEX_RE.match(text) or len(text) != 16:
        return ""
    return "0x" + text


def normalize_state(raw: Any) -> str:
    return re.sub(r"\s+", " ", str(raw or "").strip()).lower()


def controller_identity(controller: Any) -> Dict[str, Any]:
    """从 core.ControllerInfo 提取稳定身份。"""

    vendor = getattr(getattr(controller, "vendor", None), "value", "") or "generic"
    serial = str(getattr(controller, "serial", "") or "").strip()
    index = int(getattr(controller, "index", 0) or 0)
    controller_id = f"{vendor}:{serial}" if serial else f"{vendor}:unknown-c{index}"
    return {
        "controller_id": controller_id,
        "vendor": vendor,
        "model": str(getattr(controller, "model", "") or ""),
        "serial": serial,
        "firmware": str(getattr(controller, "firmware", "") or ""),
        "tool": str(getattr(controller, "tool", "") or ""),
        "binary": str(getattr(controller, "binary", "") or ""),
        "index": index,
        "destructive_allowed": bool(serial),
    }


def canonical_json(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha256_canonical(obj: Any) -> str:
    return hashlib.sha256(canonical_json(obj)).hexdigest()


def topology_projection(
    controllers: Iterable[Mapping[str, Any]],
    pds: Iterable[Mapping[str, Any]],
    vds: Iterable[Mapping[str, Any]],
) -> Dict[str, Any]:
    """构造 topology_hash 的 canonical 投影。"""

    controller_items: List[Dict[str, Any]] = []
    for controller in controllers:
        controller_items.append(
            {
                "controller_id": str(controller.get("controller_id", "")),
                "vendor": str(controller.get("vendor", "")),
                "model": str(controller.get("model", "")),
                "serial": str(controller.get("serial", "")),
                "firmware": str(controller.get("firmware", "")),
                "tool": str(controller.get("tool", "")),
            }
        )
    controller_items.sort(key=lambda item: item["controller_id"])

    pd_items: List[Dict[str, Any]] = []
    for disk in pds:
        pd_items.append(
            {
                "controller_id": str(disk.get("controller_id", "")),
                "serial": str(disk.get("serial", "")),
                "wwn": normalize_wwn(disk.get("wwn", "")),
                "size_bytes": int(disk.get("size_bytes", 0) or 0),
                "state": normalize_state(disk.get("raid_state", disk.get("state", ""))),
            }
        )
    pd_items.sort(
        key=lambda item: (
            item["controller_id"],
            item["serial"] or item["wwn"],
            item["size_bytes"],
        )
    )

    vd_items: List[Dict[str, Any]] = []
    for volume in vds:
        members = volume.get("member_serials", []) or []
        vd_items.append(
            {
                "controller_id": str(volume.get("controller_id", "")),
                "vd_id": str(volume.get("vd_id", "")),
                "vd_name": str(volume.get("vd_name", "")),
                "raid_level": str(volume.get("raid_level", "")),
                "size_bytes": int(volume.get("size_bytes", 0) or 0),
                "member_serials": sorted(str(item) for item in members),
            }
        )
    vd_items.sort(key=lambda item: (item["controller_id"], item["vd_id"]))

    return {"controllers": controller_items, "pds": pd_items, "vds": vd_items}


def topology_hash(projection: Mapping[str, Any]) -> str:
    return sha256_canonical(dict(projection))


def topology_diff(before: Mapping[str, Any], after: Mapping[str, Any]) -> Dict[str, Any]:
    def _key_pd(item: Mapping[str, Any]) -> str:
        return f"{item.get('controller_id')}::{item.get('serial') or item.get('wwn')}"

    def _key_vd(item: Mapping[str, Any]) -> str:
        return f"{item.get('controller_id')}::{item.get('vd_id')}"

    before_pds = {_key_pd(item): item for item in before.get("pds", [])}
    after_pds = {_key_pd(item): item for item in after.get("pds", [])}
    before_vds = {_key_vd(item): item for item in before.get("vds", [])}
    after_vds = {_key_vd(item): item for item in after.get("vds", [])}

    return {
        "pds_added": sorted(set(after_pds) - set(before_pds))[:20],
        "pds_removed": sorted(set(before_pds) - set(after_pds))[:20],
        "pds_changed": sorted(
            key
            for key in set(before_pds) & set(after_pds)
            if before_pds[key] != after_pds[key]
        )[:20],
        "vds_added": sorted(set(after_vds) - set(before_vds))[:20],
        "vds_removed": sorted(set(before_vds) - set(after_vds))[:20],
        "vds_changed": sorted(
            key
            for key in set(before_vds) & set(after_vds)
            if before_vds[key] != after_vds[key]
        )[:20],
    }


__all__ = [
    "canonical_json",
    "controller_identity",
    "normalize_state",
    "normalize_wwn",
    "sha256_canonical",
    "topology_diff",
    "topology_hash",
    "topology_projection",
]
