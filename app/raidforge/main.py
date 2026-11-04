"""Entry point: bootstrap -> TUI -> evidence finalisation -> upload.

Two modes exist:

``wrapper`` mode (default)
    ``asciinema rec session.cast -c "python3 -m raidforge"`` wraps the whole
    session, so startup, interaction and shutdown are all recorded.  The
    ``RAIDFORGE_APP_INNER=1`` marker stops us recursing.

``direct`` mode
    The TUI runs in this process; used when the recorder is missing or when
    ``--no-record`` is given.  In both modes the code path after app exit is
    identical: write ``manifest.json``, then ship the session (rsync -> HTTP ->
    pending queue).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .config import Config, load_config
from .core.evidence import EvidenceSession
from .core.publish import PublishOptions, publish_from_context
from .core.record import is_inner_process, recorder_available, run_recorded
from .core.report import retry_pending
from .core.serverid import ServerId, resolve_server_id
from .ui.app import create_app
from .ui.state import AppContext, AppState

log = logging.getLogger("raidforge")

EXIT_OK = 0
EXIT_CONFIG_ERROR = 2
EXIT_APP_ERROR = 1


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    """Parse command line options."""

    parser = argparse.ArgumentParser(prog="raidforge", description="RAIDFORGE RAID management and disk erase TUI")
    parser.add_argument("--config", help="extra YAML config file consulted last")
    parser.add_argument("--dry-run", action="store_true", help="never submit anything to a controller")
    parser.add_argument("--no-record", action="store_true", help="disable asciinema recording")
    parser.add_argument("--no-upload", action="store_true", help="do not attempt to ship the evidence")
    parser.add_argument(
        "--set",
        action="append",
        default=[],
        metavar="SECTION.KEY=VALUE",
        help="override one config value, e.g. --set report.http_base_url=http://10.0.0.1:8080",
    )
    parser.add_argument("--print-plan", action="store_true", help="print the dry run plan for every disk and exit")
    parser.add_argument(
        "--print-raid-plan",
        action="store_true",
        help="print detected controllers, RAID topology and free-disk eligibility, then exit",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    return parser.parse_args(argv)


def parse_overrides(pairs: List[str]) -> Dict[str, Any]:
    """Turn ``--set section.key=value`` entries into a nested override dict."""

    overrides: Dict[str, Any] = {}
    for pair in pairs:
        key, _, value = pair.partition("=")
        if not key or not value:
            continue
        if "." in key:
            section, field = key.split(".", 1)
            overrides.setdefault(section, {})[field] = value
        else:
            overrides[key] = value
    return overrides


def build_config(args: argparse.Namespace) -> Config:
    """Load configuration, applying CLI overrides on top."""

    overrides = parse_overrides(args.set)
    cfg = load_config(extra_path=args.config, overrides=overrides)
    if args.dry_run:
        cfg.dry_run = True
    if args.no_record:
        cfg.evidence.record_session = False
    if args.no_upload:
        cfg.report.enabled = False
    return cfg


def build_context(cfg: Config, server_id: Optional[ServerId] = None, evidence: Optional[EvidenceSession] = None) -> AppContext:
    """Assemble the shared context handed to the UI."""

    server_id = server_id or resolve_server_id()
    wrapper = _WrapperState.load()
    evidence = evidence or EvidenceSession(cfg, server_id.serial, session_id=wrapper.session_id)
    state = AppContext(cfg=cfg, evidence=evidence, server_id=server_id)
    AppState.activate(state)
    return state


@dataclass(frozen=True)
class _WrapperState:
    """Session identity handed from the recording wrapper to its child.

    Without this, the child process (which re-runs :func:`main`) would mint a
    brand new session id and write its evidence next to - instead of inside -
    the directory that already holds ``session.cast``.
    """

    session_id: Optional[str]
    server_sn: Optional[str]

    EXPORT_SESSION_ID = "RAIDFORGE_SESSION_ID"
    EXPORT_SERVER_SN = "RAIDFORGE_SERVER_SN"

    @classmethod
    def load(cls) -> "_WrapperState":
        return cls(session_id=os.environ.get(cls.EXPORT_SESSION_ID), server_sn=os.environ.get(cls.EXPORT_SERVER_SN))

    @classmethod
    def export(cls, ctx: AppContext) -> None:
        os.environ[cls.EXPORT_SESSION_ID] = ctx.evidence.session_id
        os.environ[cls.EXPORT_SERVER_SN] = ctx.server_id.serial


def start_background_flush(cfg: Config) -> Optional[threading.Thread]:
    """Retry queued sessions off the critical path.

    Returns:
        The daemon thread, or ``None`` when reporting is disabled.
    """

    if not cfg.report.enabled:
        return None

    def _worker() -> None:
        try:
            results = retry_pending(cfg)
            uploaded = sum(1 for result in results if result.ok)
            if uploaded:
                log.info("flushed %s queued session(s)", uploaded)
        except Exception:  # pragma: no cover - never crash the TUI for this
            log.exception("pending flush failed")

    thread = threading.Thread(target=_worker, name="pending-flush", daemon=True)
    thread.start()
    return thread


def _console(message: str) -> None:
    """Print one progress line on the (already restored) console."""

    try:
        sys.stderr.write(message + "\n")
        sys.stderr.flush()
    except Exception:  # pragma: no cover - stdout may be gone
        pass


def finalize_session(ctx: AppContext, publish: bool = False) -> Dict[str, Any]:
    """Finalise the evidence manifest.

    Report generation and upload are part of the erase task, not application
    exit.  By default this only waits for background work and re-hashes the
    finished session.  ``publish=True`` is retained for legacy callers/tests
    that explicitly want the old report/upload pass.

    Args:
        ctx: Shared context holding the evidence session.
        publish: Also build the report and upload the session.

    Returns:
        The final manifest dict (handy for logging / tests).
    """

    # Any worker still running could append to events.jsonl *after* the manifest
    # is hashed, which would invalidate the whole evidence chain.
    if not ctx.wait_idle(5.0):
        log.info("background publisher still running after 5s - waiting for the publish lock")

    if publish:
        options = PublishOptions(
            phase="exit",
            upload=ctx.cfg.report.enabled and not is_inner_process(),
            queue_on_failure=True,
            budget_seconds=ctx.cfg.report.exit_upload_seconds,
            skip_route_wait=ctx.network_ready,
            on_status=lambda line: _console(f"  {line}"),
        )
    else:
        options = PublishOptions(
            phase="manifest",
            upload=False,
            build_report=False,
            queue_on_failure=False,
            skip_route_wait=True,
            on_status=lambda line: _console(f"  {line}"),
        )
    outcome = publish_from_context(ctx, options)
    if outcome.report_path is not None:
        _console(f"  report: {outcome.report_path.name}")
    if outcome.upload is not None:
        log.info(
            "upload via %s ok=%s queued=%s %s",
            outcome.upload.channel,
            outcome.upload.ok,
            outcome.upload.queued,
            outcome.upload.message,
        )
    _console(f"evidence: {ctx.evidence.root}")
    return outcome.manifest


def _inner_argv(args: argparse.Namespace) -> List[str]:
    """Rebuild argv for the child process when wrapping with asciinema."""

    argv = [sys.executable, "-m", "raidforge"]
    if args.config:
        argv += ["--config", args.config]
    if args.dry_run:
        argv.append("--dry-run")
    if args.no_upload:
        argv.append("--no-upload")
    for pair in args.set:
        argv += ["--set", pair]
    # Always mark the child so it does not record itself again.
    argv.append("--no-record")
    return argv


def run_direct(ctx: AppContext) -> int:
    """Run the TUI in this process."""

    from pathlib import Path

    from .ui import logbus

    app = create_app(ctx)
    logbus.install(Path("/var/log/raidforge-agent/ui.log"))
    try:
        app.run(mouse=False)
    finally:
        logbus.restore()
    manifest = finalize_session(ctx, publish=False)
    log.info("session %s finalised with %s files", ctx.evidence.session_id, manifest.get("file_count"))
    return EXIT_OK


def run_wrapped(ctx: AppContext, args: argparse.Namespace) -> int:
    """Run the TUI inside an asciinema recording."""

    inner_argv = _inner_argv(args)
    _WrapperState.export(ctx)
    ctx.evidence.emit("recording_started", recorder=ctx.cfg.evidence.recorder, argv=inner_argv)
    code = run_recorded(inner_argv, ctx.evidence.cast_path, ctx.cfg)
    ctx.evidence.emit("recording_finished", exit_code=code)
    manifest = finalize_session(ctx, publish=False)
    log.info("session %s finalised with %s files", ctx.evidence.session_id, manifest.get("file_count"))
    return EXIT_OK if code == 0 else EXIT_APP_ERROR


def print_plan(ctx: AppContext) -> int:
    """Print the dry-run plan for the current inventory and exit."""

    from .core import commands as cmd_mod
    from .core import detect as detect_mod
    from .core import enum_disks as enum_mod

    detection = detect_mod.detect_controllers(ctx.cfg)
    disks, _raw = enum_mod.enumerate_disks(ctx.cfg, detection)
    print(f"tool={detection.tool} controllers={len(detection.controllers)} disks={len(disks)}")
    for disk in disks:
        if not disk.selectable:
            print(f"  {disk.disk_id:<22} [UNKNOWN media - not erasable]")
            continue
        plan = cmd_mod.build_command(disk, cmd_mod.choose_default_method(disk, ctx.cfg), ctx.cfg)
        print(f"  {disk.disk_id:<22} {plan.label:<20} {plan.dry_run_text}")
    return EXIT_OK


def print_raid_plan(ctx: AppContext) -> int:
    """Print controller/RAID topology and free-disk eligibility, then exit.

    Read-only: no RAID command is constructed or submitted.  This is the
    headless companion to the TUI dry-run and is used by the detection test.
    """

    from .core.scan import scan_host

    scan = scan_host(ctx.cfg)
    detection = scan.detection
    raid = scan.raid
    print(f"tool={detection.tool} binary={detection.binary or '-'} controllers={len(detection.controllers)}")
    for controller in detection.controllers:
        topology = raid.topology(controller.index)
        caps = raid.capabilities_for(controller.index)
        print(
            f"  {controller.controller_id}: {controller.vendor.label} model={controller.model or '-'} "
            f"fw={controller.firmware or '-'} binary={controller.binary or '-'}"
        )
        print(
            "    capabilities: "
            + ", ".join(
                [
                    f"create={caps.create}",
                    f"delete={caps.delete_vd}",
                    f"clear={caps.clear_config}",
                    f"levels={','.join(caps.levels) or '-'}",
                ]
            )
        )
        if topology is None:
            print("    topology: not collected")
            continue
        if topology.error:
            print(f"    topology ERROR: {topology.error}")
        print(f"    volumes: {len(topology.volumes)}")
        for volume in topology.volumes:
            print(
                f"      {volume.volume_id:<6} {volume.level_label:<7} {volume.state or '-':<10} "
                f"{volume.size_human:<10} members={volume.members_label}"
            )
        print(f"    free disks: {', '.join(topology.free_disk_ids) or '-'}")
        for disk_id, reason in topology.blocked.items():
            print(f"      blocked {disk_id}: {reason}")
    return EXIT_OK


def main(argv: Optional[List[str]] = None) -> int:
    """Process entry point.

    Returns:
        Process exit code.
    """

    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    try:
        cfg = build_config(args)
        ctx = build_context(cfg)
    except Exception as exc:  # pragma: no cover - catastrophic misconfiguration
        logging.exception("cannot start: %s", exc)
        return EXIT_CONFIG_ERROR

    ctx.evidence.emit(
        "session_started",
        app_version=cfg.app_version,
        dry_run=cfg.dry_run,
        hostname=_hostname(),
        server_id=ctx.server_id.to_json_obj(),
        python=sys.version.split()[0],
        argv=list(argv or sys.argv[1:]),
    )
    ctx.evidence.write_json("server_id.json", ctx.server_id.to_json_obj())
    log.info("session %s started for SN %s", ctx.evidence.session_id, ctx.server_id.serial)

    if args.print_plan:
        return print_plan(ctx)
    if args.print_raid_plan:
        return print_raid_plan(ctx)

    start_background_flush(cfg)

    use_recorder = (
        cfg.evidence.record_session
        and not args.no_record
        and not is_inner_process()
        and recorder_available(cfg)
    )
    try:
        if use_recorder:
            return run_wrapped(ctx, args)
        return run_direct(ctx)
    except KeyboardInterrupt:  # pragma: no cover
        ctx.evidence.emit("interrupted")
        finalize_session(ctx, publish=False)
        return EXIT_APP_ERROR
    except Exception as exc:  # pragma: no cover
        ctx.evidence.emit("fatal", error=str(exc))
        logging.exception("fatal error")
        finalize_session(ctx, publish=False)
        return EXIT_APP_ERROR


def _hostname() -> str:
    """Best effort hostname for the evidence header."""

    try:
        import socket

        return socket.gethostname()
    except Exception:  # pragma: no cover
        return "unknown"


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
