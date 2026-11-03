"""Shared refresh routine used by the Dashboard and the RAID screens."""

from __future__ import annotations

import json

from ..core.scan import ScanResult, scan_host
from .state import AppContext


def run_scan(ctx: AppContext) -> ScanResult:
    """Scan the host, update ``ctx`` and archive the raw vendor output."""

    with ctx.raid_lock:
        result = scan_host(ctx.cfg)
    ctx.apply_scan(result)

    ctx.evidence.write_controller_raw(
        [("detection.json", json.dumps(result.detection.to_json_obj(), indent=2, ensure_ascii=False))]
        + list(result.raw_outputs)
    )
    ctx.evidence.emit("detection", **result.detection.to_json_obj())
    ctx.evidence.emit(
        "inventory",
        disk_count=len(result.disks),
        disks=[disk.to_json_obj() for disk in result.disks],
    )
    ctx.evidence.emit("raid_topology", **result.raid.to_json_obj())
    return result


__all__ = ["run_scan"]
