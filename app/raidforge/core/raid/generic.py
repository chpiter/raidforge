"""Generic direct-attached fallback (sg3_utils / smartctl)."""

from __future__ import annotations

from typing import List, Tuple

from ...config import Config
from ..models import (
    ControllerInfo,
    DiskInfo,
    RaidCapabilities,
    RaidCommandPlan,
    RaidLayout,
    RaidTopology,
    Vendor,
    VolumeInfo,
)
from .base import RaidProvider
from .layout import sort_disks


class GenericProvider(RaidProvider):
    """Read-only provider for disks that are not behind a known RAID CLI."""

    name = "generic"
    provider_key = "generic"

    def capabilities(self, controller: ControllerInfo) -> RaidCapabilities:
        return RaidCapabilities(
            levels=[],
            create=False,
            delete_vd=False,
            clear_config=False,
            raid0_per_disk=False,
            name_supported=False,
            stripe_supported=False,
            notes=["Direct-attached path: hardware RAID management is not available"],
        )

    def collect(
        self, cfg: Config, controller: ControllerInfo, disks: List[DiskInfo]
    ) -> Tuple[RaidTopology, List[Tuple[str, str]]]:
        controller_disks = sort_disks(
            [disk for disk in disks if disk.controller_index == controller.index and disk.vendor is Vendor.GENERIC]
        )
        blocked = {
            disk.disk_id: "RAID management not supported on the generic (sg3_utils) path"
            for disk in controller_disks
        }
        topology = RaidTopology(
            controller_index=controller.index,
            vendor=Vendor.GENERIC,
            tool=controller.tool or self.name,
            binary=self.binary,
            volumes=[],
            membership={},
            free_disk_ids=[],
            blocked=blocked,
            disk_states={disk.disk_id: disk.raid_state for disk in controller_disks},
            error="",
        )
        return topology, []

    def create_plans(self, controller: ControllerInfo, layout: RaidLayout) -> List[RaidCommandPlan]:
        raise ValueError("RAID creation is not available on the generic (sg3_utils) path")

    def delete_plans(self, controller: ControllerInfo, volumes: List[VolumeInfo]) -> List[RaidCommandPlan]:
        raise ValueError("RAID deletion is not available on the generic (sg3_utils) path")

    def clear_plans(self, controller: ControllerInfo) -> List[RaidCommandPlan]:
        raise ValueError("RAID configuration is not available on the generic (sg3_utils) path")


__all__ = ["GenericProvider"]
