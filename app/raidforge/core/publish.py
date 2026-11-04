"""One shared publish path: cumulative DOCX, manifest, optional upload.

Both the per-run hook (TUI still alive) and the final exit path use this module,
so the report/manifest/upload rules cannot drift.  The module has no Textual
imports; UI feedback is injected through ``PublishOptions.on_status``.

Ordering matters for the evidence chain:

* the DOCX is written before the manifest that hashes it;
* a first manifest is written before upload so the shipped copy contains the
  report hash;
* the upload event is appended, then the manifest is written again, so the
  local manifest attests the upload result (the shipped copy cannot attest the
  upload that carries it).
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ..config import Config
from .erase_report import build_erase_report, discard_stale_report_files
from .evidence import EvidenceSession
from .models import DiskInfo, EraseRun
from .report import UploadResult, upload_session


@dataclass(frozen=True)
class PublishOptions:
    """How one publish attempt should behave."""

    phase: str = "exit"  # "run" | "exit" | "manifest"
    upload: bool = False
    #: Build the DOCX.  ``False`` only re-hashes/updates the manifest.
    build_report: bool = True
    queue_on_failure: bool = True
    budget_seconds: Optional[float] = None
    skip_route_wait: bool = False
    on_status: Optional[Callable[[str], None]] = None


@dataclass
class PublishOutcome:
    """Result of a publish attempt; never raised to the caller."""

    phase: str
    manifest: Dict[str, Any] = field(default_factory=dict)
    report_path: Optional[Path] = None
    report_error: str = ""
    upload: Optional[UploadResult] = None
    upload_error: str = ""
    error: str = ""
    duration_sec: float = 0.0


@dataclass
class PublishState:
    """Cheap status shared with the TUI (written by the publisher thread)."""

    status: str = ""
    generation: int = 0
    ok: Optional[bool] = None
    report_file: str = ""
    upload_ok: Optional[bool] = None
    queued: bool = False

    def set_status(self, text: str) -> None:
        self.status = text

    def record(self, outcome: PublishOutcome) -> None:
        self.generation += 1
        self.report_file = outcome.report_path.name if outcome.report_path else ""
        upload_ok = outcome.upload.ok if outcome.upload is not None else None
        self.upload_ok = upload_ok
        self.queued = bool(outcome.upload.queued) if outcome.upload is not None else False
        self.ok = not outcome.error and not outcome.report_error and (
            outcome.upload is None or bool(upload_ok)
        )


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def inherited_meta(evidence: EvidenceSession) -> Dict[str, Any]:
    """Carry a child TUI's manifest facts through the wrapper parent's pass."""

    try:
        data = json.loads(evidence.manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}

    meta: Dict[str, Any] = {}
    for key in ("erase_runs", "raid_runs", "submissions", "inventory"):
        if isinstance(data.get(key), list):
            meta[key] = data[key]
    if isinstance(data.get("tool"), str):
        meta["tool"] = data["tool"]
    if isinstance(data.get("disk_count"), int):
        meta["disk_count"] = data["disk_count"]

    name = data.get("report_file")
    if not isinstance(name, str) or not name or Path(name).name != name:
        return meta
    if not name.endswith(".docx") or "-raidforge-report-" not in name:
        return meta
    report_path = evidence.root / name
    if not report_path.is_file():
        return meta
    meta["path"] = report_path
    meta["report_file"] = name
    if isinstance(data.get("report_generated_at"), str):
        meta["report_generated_at"] = data["report_generated_at"]
    return meta


def inventory_payload(disks: List[DiskInfo]) -> List[Dict[str, Any]]:
    """Manifest-friendly disk list without raw vendor payloads."""

    payload: List[Dict[str, Any]] = []
    for disk in disks:
        item = disk.to_json_obj()
        item.pop("raw", None)
        payload.append(item)
    return payload


def _run_disks(runs: List[EraseRun]) -> List[DiskInfo]:
    seen: set[str] = set()
    disks: List[DiskInfo] = []
    for run in runs:
        for plan in run.plans:
            disk = run.disks.get(plan.disk_id)
            if disk is not None and disk.disk_id not in seen:
                seen.add(disk.disk_id)
                disks.append(disk)
    return disks


def _raid_summary(runs: List[Any]) -> List[Dict[str, Any]]:
    """Compact RAID action summary for the manifest (details stay in raid_actions.json)."""

    summary: List[Dict[str, Any]] = []
    for run in runs:
        summary.append(
            {
                "index": getattr(run, "index", 0),
                "epoch": getattr(run, "epoch", 0),
                "action": getattr(getattr(run, "action", None), "value", ""),
                "controller_index": getattr(run, "controller_index", 0),
                "started_at": getattr(run, "started_at", ""),
                "finished_at": getattr(run, "finished_at", ""),
                "dry_run": bool(getattr(run, "dry_run", False)),
                "cancelled": bool(getattr(run, "cancelled", False)),
                "counts": run.status_counts() if hasattr(run, "status_counts") else {},
                "observed": [getattr(result, "observed", "not_checked") for result in getattr(run, "results", [])],
            }
        )
    return summary


def _session_meta(
    source: Any,
    runs: List[EraseRun],
    inventory_disks: List[DiskInfo],
    inherited: Dict[str, Any],
    report_path: Optional[Path],
    report_generated_at: Optional[str],
    report_error: str,
) -> Dict[str, Any]:
    payload = inventory_payload(inventory_disks) or inherited.get("inventory", [])
    detected_tool = source.detection.tool if getattr(source, "detection", None) else ""
    tool_name = detected_tool or inherited.get("tool") or "none"
    submissions = [
        result.to_json_obj() for run in runs for result in run.results
    ] or inherited.get("submissions", [])
    raid_runs = _raid_summary(list(getattr(source, "raid_history", [])))
    disk_count = len(payload) or inherited.get("disk_count", 0)
    meta: Dict[str, Any] = {
        "server_id_source": source.server_id.source,
        "confirm_token": source.server_id.confirm_token,
        "tool": tool_name,
        "disk_count": disk_count,
        "submissions": submissions,
        "inventory": payload,
        "erase_runs": [run.to_json_obj() for run in runs] or inherited.get("erase_runs", []),
        "raid_runs": raid_runs or inherited.get("raid_runs", []),
        "report_file": report_path.name if report_path is not None else inherited.get("report_file"),
        "report_generated_at": report_generated_at or inherited.get("report_generated_at"),
    }
    if report_error:
        meta["report_error"] = report_error
        meta["report_stale"] = True
    return meta


def publish(source: Any, options: PublishOptions, status: Callable[[str], None]) -> PublishOutcome:
    """Build report/manifest and optionally upload.  Caller holds the lock."""

    cfg: Config = source.cfg
    evidence: EvidenceSession = source.evidence
    started = time.monotonic()

    runs = list(getattr(source, "erase_history", []))
    snapshots = list(getattr(source, "snapshot_log", []))
    inventory_disks = list(getattr(source, "inventory", []) or getattr(source, "disks", []))
    if not inventory_disks and runs:
        inventory_disks = _run_disks(runs)

    discard_stale_report_files(evidence.root)
    inherited = inherited_meta(evidence)
    report_path: Optional[Path] = inherited.get("path")
    report_generated_at: Optional[str] = inherited.get("report_generated_at")
    report_error = ""

    if runs and options.build_report:
        status(f"report: building {len(runs)} run(s) ...")
        try:
            report_path = build_erase_report(
                server_sn=source.server_id.serial,
                session_id=evidence.session_id,
                session_started_at=evidence.started_at,
                out_dir=evidence.root,
                runs=runs,
                snapshots=snapshots,
                inventory=inventory_disks,
                app_version=cfg.app_version,
                tool=(source.detection.tool if getattr(source, "detection", None) else "none"),
            )
            if report_path is None:  # pragma: no cover - builder guards empty runs
                raise RuntimeError("report builder returned no path")
            report_generated_at = _now_iso()
            duration_ms = int((time.monotonic() - started) * 1000)
            evidence.emit(
                "report_generated",
                phase=options.phase,
                file=report_path.name,
                runs=len(runs),
                screenshots=len(snapshots),
                duration_ms=duration_ms,
            )
            status(f"report ready: {report_path.name}")
        except Exception as exc:
            report_error = str(exc)[:500]
            status(f"report FAILED: {report_error}")
            evidence.emit(
                "report_failed",
                phase=options.phase,
                error=report_error,
                runs=len(runs),
            )

    meta = _session_meta(
        source,
        runs,
        inventory_disks,
        inherited,
        report_path,
        report_generated_at,
        report_error,
    )
    manifest = evidence.finalize(meta)

    upload: Optional[UploadResult] = None
    upload_error = ""
    if options.upload:
        budget_source = (
            options.budget_seconds
            if options.budget_seconds is not None
            else cfg.report.exit_upload_seconds
        )
        try:
            budget = max(1.0, min(30.0, float(budget_source)))
        except (TypeError, ValueError):
            budget = 30.0
        status(f"uploading (max {budget:.0f}s) ...")
        deadline = time.monotonic() + budget
        try:
            upload = upload_session(
                cfg,
                evidence.root,
                deadline=deadline,
                on_status=status,
                queue_on_failure=options.queue_on_failure,
                skip_route_wait=options.skip_route_wait,
            )
        except Exception as exc:  # pragma: no cover - upload_session is defensive
            upload_error = str(exc)[:500]
            upload = UploadResult("none", False, f"upload crashed: {exc}")
        upload_payload = upload.to_dict()
        upload_payload.update(phase=options.phase, runs=len(runs))
        evidence.emit("upload", **upload_payload)
        manifest = evidence.finalize({**meta, "last_upload": upload.to_dict()})
        if upload.ok:
            status(f"upload ok ({upload.channel})")
        elif upload.queued:
            status("upload FAILED - queued for next boot")
        else:
            status("upload FAILED - retry/queue at exit")
    elif options.phase == "exit":
        status("upload disabled - session remains local")

    return PublishOutcome(
        phase=options.phase,
        manifest=manifest,
        report_path=report_path,
        report_error=report_error,
        upload=upload,
        upload_error=upload_error,
        duration_sec=time.monotonic() - started,
    )


def publish_from_context(source: Any, options: PublishOptions) -> PublishOutcome:
    """Serialise publishes through ``source.publish_lock`` and catch errors."""

    state: Optional[PublishState] = getattr(source, "publish_state", None)

    def status(text: str) -> None:
        if state is not None:
            state.set_status(text)
        if options.on_status is not None:
            try:
                options.on_status(text)
            except Exception:  # pragma: no cover - UI may be shutting down
                pass

    lock: Optional[threading.Lock] = getattr(source, "publish_lock", None)
    started = time.monotonic()
    try:
        if lock is not None:
            if lock.locked():
                status("waiting for previous publish ...")
            lock.acquire()
        try:
            outcome = publish(source, options, status)
        finally:
            if lock is not None:
                lock.release()
    except Exception as exc:  # pragma: no cover - publish is defensive
        outcome = PublishOutcome(
            phase=options.phase,
            error=f"{type(exc).__name__}: {exc}"[:500],
            duration_sec=time.monotonic() - started,
        )
    outcome.duration_sec = time.monotonic() - started
    if state is not None:
        state.record(outcome)
    return outcome
