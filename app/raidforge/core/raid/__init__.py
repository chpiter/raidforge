"""RAID provider registry and topology collection."""

from __future__ import annotations

from dataclasses import replace
from typing import Dict, List, Tuple

from ...config import Config
from ..detect import normalize_tool
from ..models import (
    ControllerInfo,
    DetectionResult,
    DiskInfo,
    RaidBundle,
    RaidCapabilities,
    RaidTopology,
    Vendor,
)
from .adaptec import AdaptecProvider
from .base import RaidProvider
from .generic import GenericProvider
from .hpe import HpeProvider
from .lsi import LsiProvider

__all__ = ["collect_raid", "provider_for"]


def provider_for(detection: DetectionResult) -> RaidProvider:
    """Return the provider that speaks the detected tool's grammar."""

    tool = normalize_tool(detection.tool)
    binary = detection.binary
    if tool in ("storcli", "perccli"):
        return LsiProvider(binary or tool)
    if tool == "arcconf":
        return AdaptecProvider(binary or "arcconf")
    if tool == "ssacli":
        return HpeProvider(binary or "ssacli")
    return GenericProvider(binary or "sg_scan")


def _error_topology(controller: ControllerInfo, provider: RaidProvider, message: str) -> RaidTopology:
    return RaidTopology(
        controller_index=controller.index,
        vendor=controller.vendor,
        tool=controller.tool or provider.name,
        binary=provider.binary,
        error=message,
    )


def collect_raid(cfg: Config, detection: DetectionResult, disks: List[DiskInfo]) -> RaidBundle:
    """Collect normalised RAID topology for every detected controller."""

    provider = provider_for(detection)
    topologies: List[RaidTopology] = []
    capabilities: Dict[int, RaidCapabilities] = {}
    raw_outputs: List[Tuple[str, str]] = []
    bundle_error = ""

    if not detection.controllers:
        return RaidBundle(
            tool=detection.tool,
            binary=detection.binary,
            topologies=[],
            capabilities={},
            raw_outputs=[],
            error="no controller tool answered" if detection.tool in ("", "none") else "",
        )

    for controller in detection.controllers:
        controller.tool = controller.tool or detection.tool
        controller.binary = controller.binary or detection.binary
        try:
            topology, outputs = provider.collect(cfg, controller, disks)
        except Exception as exc:  # provider bugs must not break disk enumeration
            topology = _error_topology(controller, provider, f"RAID collection failed: {exc}")
            outputs = []
        topologies.append(topology)
        raw_outputs.extend(outputs)

        caps = provider.capabilities(controller)
        if topology.error:
            caps = replace(
                caps,
                create=False,
                delete_vd=False,
                clear_config=False,
                notes=list(caps.notes) + [topology.error],
            )
        capabilities[controller.index] = caps

        import json

        raw_outputs.append(
            (
                f"raid_topology_c{controller.index}.json",
                json.dumps(topology.to_json_obj(), ensure_ascii=False, indent=2, default=str),
            )
        )

    if all(topology.error for topology in topologies):
        bundle_error = "; ".join(
            f"c{topology.controller_index}: {topology.error}" for topology in topologies
        )

    return RaidBundle(
        tool=detection.tool,
        binary=detection.binary,
        topologies=topologies,
        capabilities=capabilities,
        raw_outputs=raw_outputs,
        error=bundle_error,
    )
