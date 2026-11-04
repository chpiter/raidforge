"""RaidForge Control Plane (webplane)."""

from __future__ import annotations

__version__ = "0.1.0"


def create_app(*args, **kwargs):
    from .app import create_app as _factory

    return _factory(*args, **kwargs)


__all__ = ["__version__", "create_app"]
