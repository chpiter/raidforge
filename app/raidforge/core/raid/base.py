"""RAID provider interface.

A provider owns everything vendor-specific about RAID management: reading the
topology, deciding which disks may join a new volume, and constructing the
exact vendor commands.  The UI only ever sees the normalised models.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List, Tuple

from ...config import Config
from ..models import (
    ControllerInfo,
    DiskInfo,
    RaidCapabilities,
    RaidCommandPlan,
    RaidLayout,
    RaidTopology,
    VolumeInfo,
)


class RaidProvider(ABC):
    """Base class for one vendor command grammar."""

    name: str = "generic"
    binary: str = ""
    provider_key: str = "generic"

    def __init__(self, binary: str = "") -> None:
        self.binary = binary

    @abstractmethod
    def capabilities(self, controller: ControllerInfo) -> RaidCapabilities:
        """Return what this controller/tool can do."""

    @abstractmethod
    def collect(
        self, cfg: Config, controller: ControllerInfo, disks: List[DiskInfo]
    ) -> Tuple[RaidTopology, List[Tuple[str, str]]]:
        """Read topology and return ``(topology, raw_outputs)``."""

    @abstractmethod
    def create_plans(self, controller: ControllerInfo, layout: RaidLayout) -> List[RaidCommandPlan]:
        """Build create commands; never executes them."""

    @abstractmethod
    def delete_plans(self, controller: ControllerInfo, volumes: List[VolumeInfo]) -> List[RaidCommandPlan]:
        """Build delete-VD commands; never executes them."""

    @abstractmethod
    def clear_plans(self, controller: ControllerInfo) -> List[RaidCommandPlan]:
        """Build clear-controller-configuration commands; never executes them."""


__all__ = ["RaidProvider"]
