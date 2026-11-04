"""Background expiry loop for leased / pending tasks."""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from .db import connect
from .services.tasks import expire_stale

log = logging.getLogger(__name__)
_STARTED: set[str] = set()
_LOCK = threading.Lock()


def start_maintenance(settings) -> None:
    key = str(Path(settings.db_path).resolve())
    with _LOCK:
        if key in _STARTED:
            return
        _STARTED.add(key)
    thread = threading.Thread(
        target=_loop,
        args=(settings,),
        name="webplane-maintenance",
        daemon=True,
    )
    thread.start()


def _loop(settings) -> None:
    while True:
        try:
            conn = connect(settings.db_path)
            try:
                expire_stale(conn, settings)
            finally:
                conn.close()
        except Exception as exc:  # pragma: no cover - loop must not die
            log.warning("maintenance pass failed: %s", exc)
        threading.Event().wait(max(5, int(settings.maintenance_interval)))


__all__ = ["start_maintenance"]
