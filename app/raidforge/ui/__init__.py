"""Presentation layer for the RAIDFORGE TUI.

Nothing in here talks to a vendor tool directly: the UI consumes already
normalised :class:`~raidforge.core.models.DiskInfo` objects and reports through
the shared :class:`AppContext`.
"""

from .state import AppContext

__all__ = ["AppContext"]
