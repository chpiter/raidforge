"""Thin UI-facing facade over the existing core.

No hardware, command grammar, safety gate or evidence rule is reimplemented
here.  The module only composes existing functions so pages and workers do not
reach into ``core`` directly.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from ..core import commands as erase_commands
from ..core import enum_disks
from ..core.erase import execute_plans
from ..core.models import (
    CommandPlan,
    DiskInfo,
    EraseRun,
    RaidAction,
    RaidActionResult,
    RaidActionRun,
    RaidCommandPlan,
    RaidLayout,
    RaidLevel,
    Raid0Mode,
    RaidStatus,
    RaidTopology,
    SubmitResult,
    utc_now_iso,
)
from ..core.publish import PublishOptions, publish_from_context
from ..core.raid import provider_for
from ..core.raid import layout as layout_mod
from ..core.raid.execute import execute_raid_plans
from .models import EvidenceFile, EvidenceSessionView, ReportState, ReportStatus
from .scan import run_scan
from .state import AppState


class Services:
    """Read/refresh/plan/execute wrappers used by pages and TaskRunner."""

    def __init__(self, state: AppState) -> None:
        self.state = state

    # -- refresh ------------------------------------------------------------
    def refresh_all(self) -> None:
        """Full detect + enumerate + topology collection (startup/F5 only)."""

        run_scan(self.state)

    def refresh_controller(self, controller_index: int) -> None:
        """Refresh one controller's disks and topology without touching the rest."""

        state = self.state
        if state.detection is None:
            self.refresh_all()
            return
        with state.raid_lock:
            disks, raw = enum_disks.enumerate_disks(state.cfg, state.detection)
            controller = state.controller_by_index(controller_index)
            if controller is None:
                raise ValueError(f"controller c{controller_index} is not present")
            provider = provider_for(state.detection)
            topology, outputs = provider.collect(state.cfg, controller, disks)
            capabilities = provider.capabilities(controller)
            if topology.error:
                capabilities = replace(
                    capabilities,
                    create=False,
                    delete_vd=False,
                    clear_config=False,
                    notes=list(capabilities.notes) + [topology.error],
                )
        state.replace_controller_disks(controller_index, disks)
        state.replace_topology(topology, capabilities)
        state.evidence.write_controller_raw(
            list(raw) + list(outputs) + [("detection.json", json.dumps(state.detection.to_json_obj(), indent=2, ensure_ascii=False))]
        )
        state.evidence.emit(
            "inventory",
            disk_count=len(state.physical_disks),
            disks=[disk.to_json_obj() for disk in state.physical_disks],
        )
        if state.raid is not None:
            state.evidence.emit("raid_topology", **state.raid.to_json_obj())

    def refresh_disks(self, controller_index: int) -> None:
        """Re-enumerate disks for one controller; topology is left as-is."""

        state = self.state
        if state.detection is None:
            self.refresh_all()
            return
        with state.raid_lock:
            disks, raw = enum_disks.enumerate_disks(state.cfg, state.detection)
        state.replace_controller_disks(controller_index, disks)
        state.evidence.write_controller_raw(list(raw))
        state.evidence.emit(
            "inventory",
            disk_count=len(state.physical_disks),
            disks=[disk.to_json_obj() for disk in state.physical_disks],
        )

    # -- planning -----------------------------------------------------------
    def plan_erase(self, disk_ids: List[str]) -> Tuple[List[CommandPlan], Dict[str, DiskInfo]]:
        disks = [self.state.disk_by_id(disk_id) for disk_id in disk_ids]
        selected = [disk for disk in disks if disk is not None]
        plans = erase_commands.plan_for_selection(selected, self.state.cfg)
        snapshot = {disk.disk_id: disk for disk in selected}
        return plans, snapshot

    def plan_create(
        self,
        controller_index: int,
        level: RaidLevel,
        disk_ids: List[str],
        mode: Raid0Mode = Raid0Mode.SINGLE,
        name: str = "",
    ) -> Tuple[RaidLayout, List[RaidCommandPlan], List[str]]:
        state = self.state
        controller = state.controller_by_index(controller_index)
        if controller is None:
            raise ValueError(f"controller c{controller_index} is not present")
        if state.detection is None:
            raise ValueError("no detection result")
        disks = [state.disk_by_id(disk_id) for disk_id in disk_ids]
        selected = [disk for disk in disks if disk is not None]
        layout = layout_mod.build_layout(
            level,
            selected,
            mode=mode,
            name=name,
            stripe_kb=state.cfg.raid.stripe_kb,
            name_max_len=state.cfg.raid.name_max_len,
            init=state.cfg.raid.init,
        )
        warnings = layout_mod.validate_layout(layout)
        provider = provider_for(state.detection)
        plans = provider.create_plans(controller, layout) if not warnings else []
        return layout, plans, warnings

    def plan_delete(self, controller_index: int, volume_id: str) -> List[RaidCommandPlan]:
        state = self.state
        controller = state.controller_by_index(controller_index)
        view = state.volume_by_id(volume_id, controller_index)
        if controller is None or view is None or view.raw is None:
            raise ValueError("virtual drive is not present")
        if state.detection is None:
            raise ValueError("no detection result")
        return provider_for(state.detection).delete_plans(controller, [view.raw])

    def plan_clear(self, controller_index: int) -> List[RaidCommandPlan]:
        state = self.state
        controller = state.controller_by_index(controller_index)
        if controller is None or state.detection is None:
            raise ValueError("controller is not present")
        return provider_for(state.detection).clear_plans(controller)

    # -- execution ----------------------------------------------------------
    def execute_erase(
        self,
        plans: List[CommandPlan],
        disks: Dict[str, DiskInfo],
        on_progress: Optional[Callable[[int, int, SubmitResult], None]] = None,
        should_stop: Optional[Callable[[], bool]] = None,
        epoch: int = 0,
    ) -> List[SubmitResult]:
        state = self.state
        started_at = utc_now_iso()
        results = execute_plans(
            plans,
            state.evidence,
            state.cfg,
            on_progress,
            should_stop=should_stop,
        )
        state.results = results
        stopped = bool(should_stop and should_stop())
        run = EraseRun(
            index=len(state.erase_history) + 1,
            run_epoch=epoch or state.run_epoch,
            started_at=started_at,
            finished_at=utc_now_iso(),
            dry_run=bool(state.cfg.dry_run),
            cancelled=stopped,
            plans=list(plans),
            results=list(results),
            disks={plan.disk_id: disks[plan.disk_id] for plan in plans if plan.disk_id in disks},
        )
        state.erase_history.append(run)
        state.evidence.emit(
            "erase_run_recorded",
            index=run.index,
            run_epoch=run.run_epoch,
            planned=len(run.plans),
            attempts=len(run.results),
            cancelled=stopped,
        )
        return results

    def execute_raid(
        self,
        plans: List[RaidCommandPlan],
        action: RaidAction,
        controller_index: int,
        on_progress: Optional[Callable[[int, int, RaidActionResult], None]] = None,
        should_stop: Optional[Callable[[], bool]] = None,
        epoch: int = 0,
    ) -> Tuple[List[RaidActionResult], str, str]:
        state = self.state
        started_at = utc_now_iso()
        topology_before = self._topology_dict(controller_index)
        results = execute_raid_plans(
            plans,
            state.evidence,
            state.cfg,
            on_progress,
            should_stop=should_stop,
        )
        scan_error = ""
        try:
            self.refresh_controller(controller_index)
        except Exception as exc:  # pragma: no cover - rescan failure must not hide results
            scan_error = str(exc)
        topology_after = self.state.raid_topology(controller_index)
        observed, observed_detail = self.observe(action, plans, topology_after, scan_error)
        stopped = bool(should_stop and should_stop()) and len(results) < len(plans)
        run = RaidActionRun(
            index=len(state.raid_history) + 1,
            epoch=epoch or state.raid_epoch,
            action=action,
            started_at=started_at,
            finished_at=utc_now_iso(),
            dry_run=bool(state.cfg.dry_run),
            cancelled=stopped,
            controller_index=controller_index,
            plans=list(plans),
            results=list(results),
            topology_before=topology_before,
            topology_after=topology_after.to_json_obj() if topology_after else {},
        )
        state.raid_history.append(run)
        state.evidence.write_json(
            "raid_actions.json",
            [record.to_json_obj() for record in state.raid_history],
        )
        state.evidence.emit(
            "raid_run_recorded",
            index=run.index,
            epoch=run.epoch,
            action=run.action.value,
            planned=len(run.plans),
            attempts=len(run.results),
            cancelled=stopped,
            observed=observed,
            observed_detail=observed_detail,
        )
        state.evidence.emit(
            "raid_rescan_after",
            action=action.value,
            controller_index=controller_index,
            observed=observed,
            detail=observed_detail,
        )
        return results, observed, observed_detail

    def _topology_dict(self, controller_index: int) -> dict:
        topology = self.state.raid_topology(controller_index)
        return topology.to_json_obj() if topology else {}

    def observe(
        self,
        action: RaidAction,
        plans: List[RaidCommandPlan],
        topology: Optional[RaidTopology],
        scan_error: str = "",
    ) -> Tuple[str, str]:
        """Honest post-run verdict: accepted is not finished."""

        if scan_error:
            return "not_checked", f"rescan failed: {scan_error[:120]}"
        if topology is None:
            return "not_checked", "no topology after the run"
        if topology.error:
            return "not_checked", topology.error[:160]
        if action is RaidAction.CREATE:
            wanted = [(plan.level, set(plan.disk_ids)) for plan in plans]
            actual = [(volume.level, set(volume.disk_ids)) for volume in topology.volumes]
            matched = sum(
                1
                for level, disks in wanted
                if any(a_level == level and disks <= a_disks for a_level, a_disks in actual)
            )
            detail = f"{matched}/{len(wanted)} target volume(s) visible; now {len(topology.volumes)} volume(s)"
            if matched == len(wanted):
                return "confirmed", detail
            if matched == 0:
                return "not_visible", detail + " (the controller may still be initialising)"
            return "mismatch", detail
        if action is RaidAction.DELETE_VD:
            wanted = {plan.volume_id for plan in plans}
            remaining = [volume.volume_id for volume in topology.volumes if volume.volume_id in wanted]
            if not remaining:
                return "confirmed", f"deleted {', '.join(sorted(wanted))}; no target VD remains"
            return "mismatch", f"still visible: {', '.join(remaining)}"
        if action is RaidAction.CLEAR_CONFIG:
            foreign = [
                disk_id
                for disk_id, reason in topology.blocked.items()
                if "frgn" in reason.lower() or "foreign" in reason.lower()
            ]
            if not topology.volumes and not foreign:
                return "confirmed", "no virtual drives or foreign configuration remain"
            if foreign:
                return "mismatch", f"foreign metadata still present on: {', '.join(foreign)}"
            return "mismatch", f"{len(topology.volumes)} virtual drive(s) still visible"
        return "not_checked", "unknown action"

    # -- report -------------------------------------------------------------
    def publish_report(self, on_status: Optional[Callable[[str], None]] = None) -> ReportStatus:
        state = self.state
        outcome = publish_from_context(
            state,
            PublishOptions(
                phase="run",
                upload=state.cfg.report.enabled,
                queue_on_failure=False,
                budget_seconds=state.cfg.report.run_upload_seconds,
                skip_route_wait=state.network_ready,
                on_status=on_status,
            ),
        )
        return self.report_status_from_outcome(outcome)

    def report_status_from_outcome(self, outcome) -> ReportStatus:
        report_path = str(outcome.report_path) if outcome.report_path else ""
        if outcome.report_error:
            return ReportStatus(
                state=ReportState.FAILED,
                message=outcome.report_error[:240],
                updated_at=utc_now_iso(),
                report_path=report_path,
            )
        if outcome.upload is not None:
            if outcome.upload.ok:
                report_state = ReportState.UPLOADED
            elif outcome.upload.queued:
                report_state = ReportState.QUEUED
            else:
                report_state = ReportState.FAILED
            return ReportStatus(
                state=report_state,
                channel=outcome.upload.channel,
                message=outcome.upload.message,
                updated_at=utc_now_iso(),
                queued=bool(outcome.upload.queued),
                report_path=report_path,
            )
        return ReportStatus(
            state=ReportState.GENERATED if report_path else ReportState.NONE,
            message="report generated; upload disabled" if report_path else "no report produced",
            updated_at=utc_now_iso(),
            report_path=report_path,
        )

    # -- evidence catalog ---------------------------------------------------
    def scan_evidence(self) -> List[EvidenceSessionView]:
        root = Path(self.state.cfg.evidence.root)
        if not root.is_dir():
            return []
        sessions: List[EvidenceSessionView] = []
        for directory in sorted(root.iterdir(), reverse=True):
            if not directory.is_dir():
                continue
            if not (directory / "events.jsonl").is_file() and not (directory / "manifest.json").is_file():
                continue
            sessions.append(self._read_session(directory))
        return sessions

    def _read_session(self, directory: Path) -> EvidenceSessionView:
        manifest: dict = {}
        manifest_path = directory / "manifest.json"
        if manifest_path.is_file():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except Exception:
                manifest = {}
        files: List[EvidenceFile] = []
        for path in sorted(directory.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(directory).as_posix()
            kind = "file"
            if path.name == "events.jsonl":
                kind = "events"
            elif path.suffix == ".svg":
                kind = "snapshot"
            elif path.suffix == ".docx":
                kind = "report"
            elif path.suffix == ".json":
                kind = "json"
            elif path.suffix == ".cast":
                kind = "recording"
            files.append(
                EvidenceFile(
                    rel_path=rel,
                    size=path.stat().st_size,
                    kind=kind,
                    mtime=utc_now_iso(),
                )
            )
        reports = sorted(directory.glob("*-raidforge-report-*.docx"))
        report_path = str(reports[-1]) if reports else ""
        last_upload = manifest.get("last_upload") or {}
        upload_text = ""
        report_state = ReportState.GENERATED if report_path else ReportState.NONE
        if last_upload:
            upload_text = f"{last_upload.get('channel', '-')} ok={last_upload.get('ok')} queued={last_upload.get('queued')}"
            if last_upload.get("ok"):
                report_state = ReportState.UPLOADED
            elif last_upload.get("queued"):
                report_state = ReportState.QUEUED
            else:
                report_state = ReportState.FAILED
        size_bytes = sum(item.size for item in files)
        return EvidenceSessionView(
            session_id=str(manifest.get("session_id") or directory.name),
            path=str(directory),
            started_at=str(manifest.get("started_at") or ""),
            size_bytes=size_bytes,
            file_count=int(manifest.get("file_count") or len(files)),
            manifest_ok=bool(manifest),
            report_path=report_path,
            report_state=report_state,
            upload=upload_text,
            files=files,
        )


__all__ = ["Services"]
