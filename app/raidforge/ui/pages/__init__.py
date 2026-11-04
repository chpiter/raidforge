"""Seven persistent pages mounted in the ContentSwitcher."""

from .base import StatefulPage
from .controllers import ControllersPage
from .dashboard import DashboardPage
from .evidence import EvidencePage
from .physical import PhysicalPage
from .settings import SettingsPage
from .tasks import TasksPage
from .virtual import VirtualPage

__all__ = [
    "ControllersPage",
    "DashboardPage",
    "EvidencePage",
    "PhysicalPage",
    "SettingsPage",
    "StatefulPage",
    "TasksPage",
    "VirtualPage",
]
