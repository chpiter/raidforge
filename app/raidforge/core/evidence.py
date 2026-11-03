"""Evidence writer (constraint 9).

Directory layout, per session::

    /var/log/raidforge-evidence/{server_sn}_{YYYYmmddTHHMMSSZ}/
        events.jsonl          append-only audit trail, one compact JSON per line
        session.cast          asciinema terminal recording of the whole session
        snapshots/*.svg       Textual screenshots taken at each screen change
        controller_raw/*.txt  verbatim vendor tool output, before any parsing
        manifest.json         SHA-256 inventory of everything above

``events.jsonl`` is opened per write and fsync'ed: even if the machine is pulled
mid-erase, previously emitted events are on disk.
"""

from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from ..config import Config
from .models import dumps, utc_now_iso
from .utils import ensure_dir, sha256_file

_SESSION_TS_RE = re.compile(r"_(\d{8}T\d{6}Z)$")


def session_timestamp() -> str:
    """Compact UTC timestamp used inside session id and file names."""

    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def started_at_from_session_id(session_id: str) -> Optional[str]:
    """Recover the session start from ``<serial>_YYYYmmddTHHMMSSZ``.

    The wrapper parent creates the directory; the child TUI joins the same
    session id a few seconds later.  Both processes, the report and the
    manifest must use the directory's timestamp, not their own construction
    time.
    """

    match = _SESSION_TS_RE.search(session_id or "")
    if not match:
        return None
    try:
        stamp = datetime.strptime(match.group(1), "%Y%m%dT%H%M%SZ")
    except ValueError:  # pragma: no cover - generated ids always match
        return None
    return stamp.replace(tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class EvidenceSession:
    """Owns one evidence directory for the lifetime of the session."""

    def __init__(self, cfg: Config, server_sn: str, session_id: Optional[str] = None) -> None:
        self.cfg = cfg
        self.server_sn = server_sn
        self.session_id = session_id or f"{server_sn}_{session_timestamp()}"
        self.started_at = started_at_from_session_id(self.session_id) or utc_now_iso()
        self.root = ensure_dir(Path(cfg.evidence_root) / self.session_id)
        self.snapshots_dir = ensure_dir(self.root / "snapshots")
        self.raw_dir = ensure_dir(self.root / "controller_raw")
        self.events_path = self.root / "events.jsonl"
        self.manifest_path = self.root / "manifest.json"
        self.seq = 0
        self._emit_lock = threading.Lock()

    # -- properties ---------------------------------------------------------
    @property
    def cast_path(self) -> Path:
        """Where the asciinema recording is written."""

        return self.root / "session.cast"

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<EvidenceSession {self.session_id} at {self.root}>"

    # -- events -------------------------------------------------------------
    def emit(self, event: str, **payload: Any) -> Dict[str, Any]:
        """Append one line to ``events.jsonl``.

        Args:
            event: Machine readable event name, e.g. ``erasure.submit``.
            **payload: JSON serialisable context.

        Returns:
            The exact dict that was written (handy for tests and for pushing
            the same record to the UI log).
        """

        with self._emit_lock:
            self.seq += 1
            record = {
                "seq": self.seq,
                "ts": utc_now_iso(),
                "event": event,
                "server_sn": self.server_sn,
                "session_id": self.session_id,
                **payload,
            }
            line = dumps(record)
            with self.events_path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
                handle.flush()
                try:
                    import os

                    os.fsync(handle.fileno())
                except OSError:  # pragma: no cover - some filesystems refuse fsync
                    pass
            return record

    # -- artefacts ----------------------------------------------------------
    def write_controller_raw(self, outputs: Iterable[Tuple[str, str]]) -> List[str]:
        """Archive raw vendor tool output under ``controller_raw/``."""

        written: List[str] = []
        for name, content in outputs:
            safe = name.replace("/", "_")
            target = self.raw_dir / safe
            target.write_text(content or "", encoding="utf-8")
            written.append(str(target))
        return written

    def save_svg(self, name: str, svg_text: str) -> Optional[Path]:
        """Store one Textual screenshot as SVG."""

        if not svg_text:
            return None
        safe = name.replace(" ", "_").replace("/", "_")
        if not safe.endswith(".svg"):
            safe += ".svg"
        target = self.snapshots_dir / safe
        target.write_text(svg_text, encoding="utf-8")
        return target

    def write_json(self, name: str, payload: Any) -> Path:
        """Write an arbitrary JSON document next to the other artefacts."""

        target = self.root / name
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        return target

    # -- manifest -----------------------------------------------------------
    def build_manifest(self, extra_meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Create the SHA-256 inventory of every artefact in the session."""

        files: List[Dict[str, Any]] = []
        for path in sorted(self.root.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(self.root).as_posix()
            if rel == "manifest.json":
                continue
            files.append(
                {
                    "path": rel,
                    "sha256": sha256_file(path),
                    "size": path.stat().st_size,
                }
            )
        manifest = {
            "schema": "raidforge-evidence/manifest@2",
            "session_id": self.session_id,
            "server_sn": self.server_sn,
            "started_at": self.started_at,
            "finalised_at": utc_now_iso(),
            "tool_version": self.cfg.app_version,
            "file_count": len(files),
            "files": files,
        }
        if extra_meta:
            manifest.update(extra_meta)
        return manifest

    def finalize(self, extra_meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Write ``manifest.json`` and return it.

        May be called repeatedly: each call hashes the tree as it exists now
        and overwrites the manifest.  The TUI publishes after every erase run;
        the final call after exit is the authoritative one.
        """

        manifest = self.build_manifest(extra_meta)
        self.manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return manifest
