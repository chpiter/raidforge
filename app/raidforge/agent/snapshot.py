"""主机快照：把 core 扫描结果、启动介质、硬件信息聚合成 Agent 视图。"""

from __future__ import annotations

import logging
import shutil
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..config import Config
from ..core.models import ControllerInfo, DiskInfo, VolumeInfo
from ..core.scan import ScanResult, scan_host
from .bootmedia import BootMediaMap, probe as probe_boot_media
from .clock import Clock, SystemClock, rfc3339
from .proc import Runner, resolve_binary, tool_version
from .topology import (
    controller_identity,
    normalize_wwn,
    topology_hash,
    topology_projection,
)

log = logging.getLogger(__name__)


@dataclass
class HostSnapshot:
    server_sn: str
    hardware: Dict[str, Any]
    controllers: Tuple[Dict[str, Any], ...]
    pds: Tuple[Dict[str, Any], ...]
    vds: Tuple[Dict[str, Any], ...]
    capabilities: Dict[str, Dict[str, Any]]
    boot_media: BootMediaMap
    storage: Dict[str, Any]
    projection: Dict[str, Any]
    topology_hash: str
    collected_at: str
    scan: Optional[ScanResult] = field(default=None, repr=False)
    raw_outputs: Tuple[Tuple[str, str], ...] = field(default=(), repr=False)

    # -- lookup -------------------------------------------------------------
    def controller(self, controller_id: str) -> Optional[Dict[str, Any]]:
        for item in self.controllers:
            if item.get("controller_id") == controller_id:
                return item
        return None

    def controller_info(self, controller_id: str) -> Optional[ControllerInfo]:
        if self.scan is None:
            return None
        for controller in self.scan.detection.controllers:
            if controller_identity(controller)["controller_id"] == controller_id:
                return controller
        return None

    def pds_for(self, controller_id: str) -> List[Dict[str, Any]]:
        return [item for item in self.pds if item.get("controller_id") == controller_id]

    def vds_for(self, controller_id: str) -> List[Dict[str, Any]]:
        return [item for item in self.vds if item.get("controller_id") == controller_id]

    def disk_by_id(self, disk_id: str) -> Optional[DiskInfo]:
        if self.scan is None:
            return None
        for disk in self.scan.disks:
            if disk.disk_id == disk_id:
                return disk
        return None

    def volume_by_id(self, controller_index: int, volume_id: str) -> Optional[VolumeInfo]:
        if self.scan is None:
            return None
        topology = self.scan.raid.topology(controller_index)
        if topology is None:
            return None
        for volume in topology.volumes:
            if volume.volume_id == volume_id:
                return volume
        return None

    def capabilities_for(self, controller_id: str) -> Dict[str, Any]:
        return dict(self.capabilities.get(controller_id) or {})

    def heartbeat_capabilities(self) -> Dict[str, Any]:
        return {
            "task_types": [
                "refresh_hardware",
                "list_controllers",
                "list_pds",
                "list_vds",
                "create_raid",
                "delete_raid",
                "clear_config",
                "abort_current",
            ],
            "dry_run": True,
            "evidence_kinds": [
                "command_output",
                "inventory",
                "plan",
                "screenshot",
                "report",
                "log",
                "json",
            ],
            "controllers": [
                {
                    "controller_id": item["controller_id"],
                    "vendor": item["vendor"],
                    "model": item["model"],
                    "serial": item["serial"],
                    "firmware": item["firmware"],
                    "tool": item["tool"],
                }
                for item in self.controllers
            ],
        }


class SnapshotProvider:
    """缓存扫描结果（默认 60s），但每次调用都重新探测启动介质。"""

    def __init__(
        self,
        cfg: Config,
        server_sn: str,
        evidence_dir: Path,
        *,
        clock: Optional[Clock] = None,
        runner: Optional[Runner] = None,
        cache_seconds: float = 60.0,
    ) -> None:
        self.cfg = cfg
        self.server_sn = server_sn
        self.evidence_dir = Path(evidence_dir)
        self.clock = clock or SystemClock()
        self.runner = runner or Runner()
        self.cache_seconds = cache_seconds
        self._lock = threading.RLock()
        self._cached: Optional[HostSnapshot] = None
        self._cached_at = 0.0
        self._hardware: Dict[str, Any] = {}
        self._hardware_at = 0.0

    # -- public -------------------------------------------------------------
    def capture(self, fresh: bool = False) -> HostSnapshot:
        with self._lock:
            now = self.clock.monotonic()
            if not fresh and self._cached is not None and (now - self._cached_at) <= self.cache_seconds:
                boot = probe_boot_media(self.runner)
                cached = self._cached
                cached.boot_media = boot
                return cached
            return self._capture_locked(fresh=True)

    # -- internals ----------------------------------------------------------
    def _capture_locked(self, fresh: bool) -> HostSnapshot:
        scan = scan_host(self.cfg)
        boot = probe_boot_media(self.runner)
        hardware = self._read_hardware()
        controllers = [self._controller_payload(controller) for controller in scan.detection.controllers]
        pds = [self._disk_payload(disk, scan, boot) for disk in scan.disks]
        vds = self._volume_payloads(scan)
        capabilities = {
            item["controller_id"]: {
                **scan.raid.capabilities_for(item["index"]).to_json_obj(),
                "controller_id": item["controller_id"],
            }
            for item in controllers
        }
        projection = topology_projection(controllers, pds, vds)
        storage = self._storage()
        snapshot = HostSnapshot(
            server_sn=self.server_sn,
            hardware=hardware,
            controllers=tuple(controllers),
            pds=tuple(pds),
            vds=tuple(vds),
            capabilities=capabilities,
            boot_media=boot,
            storage=storage,
            projection=projection,
            topology_hash=topology_hash(projection),
            collected_at=rfc3339(self.clock.utcnow()),
            scan=scan,
            raw_outputs=tuple(scan.raw_outputs),
        )
        self._cached = snapshot
        self._cached_at = self.clock.monotonic()
        return snapshot

    def _controller_payload(self, controller: ControllerInfo) -> Dict[str, Any]:
        identity = controller_identity(controller)
        binary = str(getattr(controller, "binary", "") or "")
        path = resolve_binary(binary)
        tool_name = str(getattr(controller, "tool", "") or binary or "")
        return {
            "controller_id": identity["controller_id"],
            "vendor": identity["vendor"],
            "model": identity["model"],
            "serial": identity["serial"],
            "firmware": identity["firmware"],
            "index": identity["index"],
            "destructive_allowed": identity["destructive_allowed"],
            "tool": {
                "name": tool_name,
                "path": path,
                "version": tool_version(path or binary, self.runner),
            },
        }

    def _disk_payload(self, disk: DiskInfo, scan: ScanResult, boot: BootMediaMap) -> Dict[str, Any]:
        controller = next(
            (item for item in scan.detection.controllers if item.index == disk.controller_index),
            None,
        )
        controller_id = controller_identity(controller)["controller_id"] if controller else ""
        topology = scan.raid.topology(disk.controller_index)
        member_of = ""
        if topology is not None:
            member_of = topology.membership.get(disk.disk_id, "")
        protected = boot.disk_for(disk.serial, disk.wwn)
        return {
            "controller_id": controller_id,
            "controller_index": disk.controller_index,
            "disk_id": disk.disk_id,
            "model": disk.model,
            "serial": disk.serial,
            "wwn": disk.wwn,
            "wwn_normalized": normalize_wwn(disk.wwn),
            "size_bytes": int(disk.size_bytes or 0),
            "medium": disk.medium.value,
            "protocol": disk.protocol.value,
            "raid_state": disk.raid_state,
            "raid_member_of": member_of,
            "device_node": disk.device_node,
            "location": disk.location,
            "boot_protected": bool(protected),
            "removable": bool(protected.removable) if protected else False,
            "mounted_at": list(protected.mountpoints) if protected else [],
        }

    def _volume_payloads(self, scan: ScanResult) -> List[Dict[str, Any]]:
        disk_by_id = {disk.disk_id: disk for disk in scan.disks}
        payloads: List[Dict[str, Any]] = []
        for topology in scan.raid.topologies:
            controller = next(
                (item for item in scan.detection.controllers if item.index == topology.controller_index),
                None,
            )
            controller_id = controller_identity(controller)["controller_id"] if controller else ""
            for volume in topology.volumes:
                members = [disk_by_id[disk_id] for disk_id in volume.disk_ids if disk_id in disk_by_id]
                payloads.append(
                    {
                        "controller_id": controller_id,
                        "controller_index": volume.controller_index,
                        "vd_id": volume.volume_id,
                        "vd_name": volume.name,
                        "raid_level": volume.level or volume.raw_level,
                        "size_bytes": int(volume.size_bytes or 0),
                        "state": volume.state,
                        "member_disk_ids": list(volume.disk_ids),
                        "member_serials": [disk.serial for disk in members],
                        "spans": [list(span) for span in volume.spans],
                        "disk_group": volume.disk_group,
                    }
                )
        return payloads

    def _storage(self) -> Dict[str, Any]:
        try:
            usage = shutil.disk_usage(str(self.evidence_dir))
            free = int(usage.free)
        except OSError:
            free = 0
        return {"evidence_dir": str(self.evidence_dir), "free_bytes": free}

    def _read_hardware(self) -> Dict[str, Any]:
        now = self.clock.monotonic()
        if self._hardware and (now - self._hardware_at) <= 300.0:
            return dict(self._hardware)
        values: Dict[str, str] = {}
        for field_name, key in (
            ("system-manufacturer", "vendor"),
            ("product-name", "model"),
            ("bios-version", "bios_version"),
        ):
            result = self.runner.run(["dmidecode", "-s", field_name], timeout=5.0)
            values[key] = result.stdout.strip().splitlines()[0].strip() if result.stdout.strip() else ""
        payload = {
            "vendor": values.get("vendor", ""),
            "model": values.get("model", ""),
            "serial_number": self.server_sn,
            "bios_version": values.get("bios_version", ""),
        }
        self._hardware = payload
        self._hardware_at = now
        return dict(payload)


__all__ = ["HostSnapshot", "SnapshotProvider"]
