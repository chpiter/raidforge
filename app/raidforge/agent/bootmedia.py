"""启动介质识别与写保护。

规则顺序与任务书 §11.4 一致：解析根分区、Live 挂载源、内核命令行、
``lsblk`` 的挂载点/可移动属性，以及承载 ``/var/lib/raidforge`` 的磁盘。
任何信息不足都返回 ``determined=False``，调用方必须 fail closed。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from .proc import Runner
from .topology import normalize_wwn

log = logging.getLogger(__name__)

LIVE_MOUNTPOINTS = ("/run/live/medium", "/lib/live/mount/medium", "/cdrom")
AGENT_STATE_PATHS = ("/var/lib/raidforge",)
_SERIAL_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def _norm_serial(raw: Any) -> str:
    text = str(raw or "").strip()
    if not text or not _SERIAL_RE.match(text):
        return ""
    return text.casefold()


@dataclass(frozen=True)
class ProtectedDisk:
    kname: str
    path: str
    serial: str = ""
    wwn: str = ""
    mountpoints: Tuple[str, ...] = ()
    removable: bool = False
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "device": self.path or self.kname,
            "serial": self.serial,
            "wwn": self.wwn,
            "is_removable": self.removable,
            "mountpoints": list(self.mountpoints),
            "reason": self.reason,
        }


@dataclass
class BootMediaMap:
    determined: bool = False
    disks: Tuple[ProtectedDisk, ...] = ()
    serials: frozenset = field(default_factory=frozenset)
    wwns: frozenset = field(default_factory=frozenset)
    root_source: str = ""
    live_sources: Tuple[str, ...] = ()
    error: str = ""

    @property
    def protected_count(self) -> int:
        return len(self.disks)

    def is_protected(self, serial: str, wwn: str) -> bool:
        serial_key = _norm_serial(serial)
        wwn_key = normalize_wwn(wwn)
        if serial_key and serial_key in self.serials:
            return True
        if wwn_key and wwn_key in self.wwns:
            return True
        return False

    def disk_for(self, serial: str, wwn: str) -> Optional[ProtectedDisk]:
        serial_key = _norm_serial(serial)
        wwn_key = normalize_wwn(wwn)
        for disk in self.disks:
            if serial_key and _norm_serial(disk.serial) == serial_key:
                return disk
            if wwn_key and normalize_wwn(disk.wwn) == wwn_key:
                return disk
        return None

    def heartbeat_payload(self) -> Dict[str, Any]:
        first = self.disks[0] if self.disks else None
        return {
            "device": first.path if first else "",
            "serial": first.serial if first else "",
            "wwn": first.wwn if first else "",
            "is_removable": bool(first.removable) if first else False,
            "mounted_at": first.mountpoints[0] if first and first.mountpoints else "",
            "determined": self.determined,
            "protected": [disk.to_dict() for disk in self.disks[:16]],
        }


def _mountpoints(node: Dict[str, Any]) -> Tuple[str, ...]:
    value = node.get("mountpoints")
    result: List[str] = []
    if isinstance(value, list):
        result.extend(str(item) for item in value if item)
    elif isinstance(value, str) and value:
        result.append(value)
    single = node.get("mountpoint")
    if isinstance(single, str) and single and single not in result:
        result.append(single)
    return tuple(result)


def _is_removable(node: Dict[str, Any]) -> bool:
    value = node.get("rm")
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes"}


def _walk_lsblk(nodes: Iterable[Dict[str, Any]], ancestor: Optional[Dict[str, Any]] = None):
    for node in nodes or []:
        if not isinstance(node, dict):
            continue
        current = node if (node.get("type") or "").lower() == "disk" else ancestor
        yield node, current
        children = node.get("children") or []
        if children:
            yield from _walk_lsblk(children, current)


def _parse_mountinfo(path: Path) -> List[Tuple[str, str]]:
    pairs: List[Tuple[str, str]] = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return pairs
    for line in lines:
        fields = line.split()
        if len(fields) < 7 or " - " not in line:
            continue
        try:
            separator = fields.index("-")
        except ValueError:
            continue
        mountpoint = fields[4].replace("\\040", " ")
        source = fields[separator + 2] if len(fields) > separator + 2 else ""
        pairs.append((mountpoint, source))
    return pairs


def _cmdline_has_live(path: Path) -> bool:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return bool(re.search(r"\b(?:boot=live|findiso=|from=)", text))


def _sysfs_disks(sys_block: Path) -> List[Dict[str, Any]]:
    disks: List[Dict[str, Any]] = []
    try:
        entries = sorted(sys_block.iterdir())
    except OSError:
        return disks
    for entry in entries:
        if not entry.is_dir():
            continue
        removable = False
        serial = ""
        wwn = ""
        try:
            removable = entry.joinpath("removable").read_text().strip() == "1"
        except OSError:
            pass
        try:
            serial = entry.joinpath("device", "serial").read_text(encoding="utf-8").strip()
        except OSError:
            pass
        try:
            wwn = entry.joinpath("wwid").read_text(encoding="utf-8").strip()
        except OSError:
            pass
        disks.append(
            {
                "kname": entry.name,
                "path": f"/dev/{entry.name}",
                "rm": removable,
                "serial": serial,
                "wwn": wwn,
                "type": "disk",
            }
        )
    return disks


def _root_disk_kname(root_source: str, disks: List[ProtectedDisk]) -> str:
    source = (root_source or "").strip()
    if not source.startswith("/dev/"):
        return ""
    candidate = source[len("/dev/") :]
    best = ""
    for disk in disks:
        name = disk.kname
        if not name:
            continue
        if candidate == name or candidate.startswith(name):
            if len(name) > len(best):
                best = name
    return best


def probe(
    runner: Optional[Runner] = None,
    *,
    mountinfo_path: Path = Path("/proc/self/mountinfo"),
    sys_block: Path = Path("/sys/block"),
    cmdline_path: Path = Path("/proc/cmdline"),
) -> BootMediaMap:
    runner = runner or Runner()
    error_parts: List[str] = []

    result = runner.run(
        ["lsblk", "-J", "-o", "PATH,KNAME,TYPE,RM,RO,SERIAL,WWN,MOUNTPOINTS"],
        timeout=10.0,
    )
    lsblk_ok = False
    raw_disks: Dict[str, Dict[str, Any]] = {}
    mounts_by_kname: Dict[str, Set[str]] = {}

    if result.return_code == 0 and result.stdout.strip():
        try:
            payload = json.loads(result.stdout)
            nodes = payload.get("blockdevices") or []
            for node, disk in _walk_lsblk(nodes):
                if disk is None:
                    continue
                kname = str(disk.get("kname") or disk.get("name") or "").strip()
                if not kname:
                    continue
                entry = raw_disks.setdefault(
                    kname,
                    {
                        "kname": kname,
                        "path": str(disk.get("path") or f"/dev/{kname}"),
                        "serial": str(disk.get("serial") or ""),
                        "wwn": str(disk.get("wwn") or ""),
                        "rm": _is_removable(disk),
                    },
                )
                points = mounts_by_kname.setdefault(kname, set())
                points.update(_mountpoints(node))
                points.update(_mountpoints(disk))
            lsblk_ok = bool(raw_disks)
        except (ValueError, TypeError) as exc:
            error_parts.append(f"lsblk JSON 解析失败: {exc}")
    else:
        error_parts.append(result.stderr.strip()[:200] or "lsblk 不可用")

    if not raw_disks:
        for entry in _sysfs_disks(sys_block):
            raw_disks[entry["kname"]] = {
                "kname": entry["kname"],
                "path": entry["path"],
                "serial": entry["serial"],
                "wwn": entry["wwn"],
                "rm": entry["rm"],
            }

    mountinfo = _parse_mountinfo(mountinfo_path)
    for mountpoint, source in mountinfo:
        if not source.startswith("/dev/"):
            continue
        source_name = source[len("/dev/") :]
        matches = [kname for kname in raw_disks if source_name == kname or source_name.startswith(kname)]
        if matches:
            kname = max(matches, key=len)
            mounts_by_kname.setdefault(kname, set()).add(mountpoint)

    live = _cmdline_has_live(cmdline_path)
    live_sources: Set[str] = set(LIVE_MOUNTPOINTS)

    disks: List[ProtectedDisk] = []
    for kname, info in raw_disks.items():
        points = tuple(sorted(mounts_by_kname.get(kname, set())))
        reasons: List[str] = []
        if points:
            reasons.append("mounted")
        if any(point in live_sources for point in points):
            reasons.append("live_medium")
        if any(any(point.startswith(prefix) for prefix in AGENT_STATE_PATHS) for point in points):
            reasons.append("agent_state")
        if info["rm"]:
            reasons.append("removable")
        if info["rm"] and any(point in live_sources for point in points):
            reasons.append("removable_live")
        if not reasons:
            continue
        disks.append(
            ProtectedDisk(
                kname=kname,
                path=str(info["path"]),
                serial=str(info["serial"] or ""),
                wwn=str(info["wwn"] or ""),
                mountpoints=points,
                removable=bool(info["rm"]),
                reason=",".join(reasons),
            )
        )

    root_source = ""
    root_result = runner.run(["findmnt", "-no", "SOURCE", "/"], timeout=5.0)
    if root_result.return_code == 0:
        root_source = root_result.stdout.strip().splitlines()[0] if root_result.stdout.strip() else ""
    if not root_source:
        for mountpoint, source in mountinfo:
            if mountpoint == "/":
                root_source = source
                break

    root_kname = _root_disk_kname(root_source, disks)
    if root_kname:
        serials = {disk.kname for disk in disks}
        if root_kname not in serials:
            info = raw_disks.get(root_kname)
            if info:
                disks.append(
                    ProtectedDisk(
                        kname=root_kname,
                        path=str(info["path"]),
                        serial=str(info["serial"] or ""),
                        wwn=str(info["wwn"] or ""),
                        mountpoints=(),
                        removable=bool(info["rm"]),
                        reason="root",
                    )
                )
    elif root_source and root_source.startswith("/dev/"):
        error_parts.append("无法把根分区映射到物理盘")

    serials = frozenset(_norm_serial(disk.serial) for disk in disks if _norm_serial(disk.serial))
    wwns = frozenset(normalize_wwn(disk.wwn) for disk in disks if normalize_wwn(disk.wwn))
    determined = bool(serials or wwns)
    if not determined:
        error_parts.append("受保护磁盘缺少 serial/wwn，无法判定启动介质")

    return BootMediaMap(
        determined=determined,
        disks=tuple(disks),
        serials=serials,
        wwns=wwns,
        root_source=root_source,
        live_sources=tuple(sorted(live_sources)),
        error="; ".join(part for part in error_parts if part),
    )


__all__ = ["BootMediaMap", "ProtectedDisk", "probe"]
