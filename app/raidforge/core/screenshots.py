"""Textual screenshots -> SVG files (constraint 9).

``App.export_screenshot()`` returns the rendered screen as an SVG string.  The
signature drifted across Textual releases (sync in some versions, awaitable in
others), so :func:`capture_svg` normalises both, plus the case where a bytes
payload comes back.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from pathlib import Path
from typing import Any, Optional

from .evidence import EvidenceSession

log = logging.getLogger(__name__)


async def capture_svg(app: Any) -> Optional[str]:
    """Return the current screen as an SVG string, or ``None`` on failure.

    Args:
        app: The running :class:`textual.app.App` instance.

    Returns:
        SVG markup, or ``None`` when the running Textual build cannot provide
        it (never raises: screenshots are evidence, not a control path).
    """

    try:
        exporter = getattr(app, "export_screenshot", None)
        if exporter is None:
            return None
        result = exporter()
        if inspect.isawaitable(result):
            result = await result
        if isinstance(result, (bytes, bytearray)):
            return bytes(result).decode("utf-8", "replace")
        if isinstance(result, str):
            return result
        return None
    except Exception as exc:  # pragma: no cover - defensive
        log.debug("export_screenshot failed: %s", exc)
        return None


async def take_snapshot(app: Any, evidence: Optional[EvidenceSession], name: str) -> Optional[Path]:
    """Capture a screenshot and archive it under ``snapshots/``.

    Args:
        app: Running Textual app.
        evidence: Target session; writes are skipped when ``None``.
        name: File stem, e.g. ``03-confirm``.

    Returns:
        Path of the written SVG, or ``None``.
    """

    svg = await capture_svg(app)
    if not svg or evidence is None:
        return None
    target = evidence.save_svg(name, svg)
    if target is not None:
        log.debug("snapshot written: %s", target)
    return target


def snapshot_sync(app: Any, evidence: Optional[EvidenceSession], name: str) -> Optional[Path]:
    """Fire-and-forget wrapper for code paths that are not async.

    Runs :func:`take_snapshot` on a dedicated loop when the caller is off-loop;
    returns ``None`` when called from inside the running loop (use the async
    form there).
    """

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(take_snapshot(app, evidence, name))
    return None
