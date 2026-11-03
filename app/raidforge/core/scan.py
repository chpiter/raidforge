"""One host scan: detect controllers, enumerate disks, collect RAID topology."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Tuple

from ..config import Config
from . import detect as detect_mod
from . import enum_disks as enum_mod
from .models import DetectionResult, DiskInfo, RaidBundle
from .raid import collect_raid


@dataclass
class ScanResult:
    """Everything a fresh scan produces, ready to be applied to the UI state."""

    detection: DetectionResult
    disks: List[DiskInfo]
    raid: RaidBundle
    raw_outputs: List[Tuple[str, str]] = field(default_factory=list)


def scan_host(cfg: Config) -> ScanResult:
    """Detect + enumerate + collect RAID topology without touching the UI."""

    detection = detect_mod.detect_controllers(cfg)
    disks, raw_outputs = enum_mod.enumerate_disks(cfg, detection)
    raid = collect_raid(cfg, detection, disks)
    return ScanResult(
        detection=detection,
        disks=disks,
        raid=raid,
        raw_outputs=list(raw_outputs) + list(raid.raw_outputs),
    )


__all__ = ["ScanResult", "scan_host"]
