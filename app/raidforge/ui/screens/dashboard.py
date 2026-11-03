"""Dashboard screen: identity, detection result, inventory summary, log tail."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal
from textual.widgets import Button, Label, Static

from ...core import detect as detect_mod
from ...core import enum_disks as enum_mod
from ...core.models import DetectionResult
from ...core.report import http_endpoint, network_summary, retry_pending, wait_for_default_route
from ..scan import run_scan
from ..state import AppContext
from ..widgets import KeyBar, SessionStatusBar
from .base import BaseScreen

SCREEN_NAME = "Dashboard"


class DashboardScreen(BaseScreen):
    """First screen shown after boot: everything the operator needs to know."""

    SCREEN_NAME = SCREEN_NAME
    #: Enter is the default only when no button is focused.  With a focused
    #: Button, its own ``enter -> press`` binding wins in the focused chain.
    BINDINGS = [Binding("enter", "go_disks", "Disk list", show=False)]
    HINTS = [
        ("Enter", "disks"),
        ("C", "raid"),
        ("R", "rescan"),
        ("U", "retry upload"),
        ("S", "settings"),
        ("Q", "quit"),
    ]

    def __init__(self, ctx: AppContext) -> None:
        super().__init__(ctx)
        self._identity = Static("")
        self._detection = Static("")
        self._summary = Static("")
        self._actions = Static("", classes="muted")

    def compose(self) -> ComposeResult:
        yield SessionStatusBar(self.ctx, SCREEN_NAME)
        with Container(id="content"):
            yield Label(f"[b]{self.ctx.cfg.app_name}[/b] v{self.ctx.cfg.app_version}", classes="title")
            yield self._identity
            yield self._detection
            yield self._summary
            with Horizontal():
                # No `variant=`: the monochrome palette has exactly two colours,
                # so a variant could only leak a third one.  Meaning lives in the
                # label and in the focus highlight.
                yield Button("Disk list [Enter]", id="go-disks")
                yield Button("RAID [C]", id="raid")
                yield Button("Rescan [R]", id="rescan")
                yield Button("Retry upload [U]", id="upload")
                yield Button("Quit [Q]", id="quit")
            yield self.status_line()
        yield KeyBar(self.HINTS)

    def on_mount(self) -> None:
        """Render static info then kick off detection and network probing."""

        self._render_identity()
        self._render_summary()
        self.set_message("scanning controllers...")
        # Deferred: a screenshot taken during mount would predate the first
        # render pass and come back empty.
        self.call_later(lambda: self.schedule_snapshot("01-dashboard"))
        # Daemon threads, not run_worker(thread=True): see AppContext.spawn.
        self.ctx.spawn(self._scan, name="scan")
        self.ctx.spawn(self._probe_network, name="probe-network")
        # Explicit default: Enter opens the disk list when no button is focused.
        self.query_one("#go-disks", Button).focus()

    # -- content builders ---------------------------------------------------
    def _render_identity(self) -> None:
        sid = self.ctx.server_id
        lines = [
            f"Server SN      : {sid.serial}  (source: {sid.source})",
            f"Session        : {self.ctx.evidence.session_id}",
            f"Erase gate     : type 'yes' in the confirm dialog",
            f"Evidence dir   : {self.ctx.evidence.root}",
        ]
        if sid.is_fallback:
            lines.append("[b]!! WARNING: DMI serial unusable - using derived identifier.[/b]")
        self._identity.update("\n".join(lines))

    def _render_summary(self) -> None:
        counts = self.ctx.counts()
        if self.ctx.network_ready:
            net = f"ready ({self.ctx.network_detail})" if self.ctx.network_detail else "ready"
        else:
            net = "waiting for DHCP"
        report = self.ctx.cfg.report
        receiver = report.ssh_host or "not set"
        if report.transport == "http":
            transport = "HTTP first, then SSH, then local queue"
        else:
            transport = "SSH first, then HTTP, then local queue"
        http = http_endpoint(self.ctx.cfg) or "not configured"
        publish = self.ctx.publish_state.status or "not published"
        raid_lines: list[str] = []
        if self.ctx.raid is None:
            raid_lines.append("RAID             : scanning...")
        else:
            volume_count = sum(len(topology.volumes) for topology in self.ctx.raid.topologies)
            raid_lines.append(
                f"RAID             : {len(self.ctx.raid.topologies)} controller(s), {volume_count} volume(s)"
            )
            if self.ctx.raid.error:
                raid_lines.append(f"RAID issue       : {self.ctx.raid.error[:90]}")
        self._summary.update(
            "\n".join(
                [
                    f"Disks discovered : {len(self.ctx.disks)}",
                    f"  HDD {counts.get('HDD', 0)}   SSD {counts.get('SSD', 0)}   "
                    f"UNKNOWN {counts.get('UNKNOWN', 0)}",
                    f"Selected         : {counts.get('SELECTED', 0)}",
                    *raid_lines,
                    f"Network          : {net}",
                    f"Transport        : {transport}",
                    f"Receiver         : {receiver}  (ssh {report.ssh_port})",
                    f"HTTP endpoint    : {http}",
                    f"Report / upload  : {publish}",
                ]
            )
        )

    def refresh_publish_status(self) -> None:
        """Repaint the summary after a background publish reports progress."""

        self._render_summary()

    def _render_detection(self, detection: DetectionResult) -> None:
        lines = [f"Tool in use      : {detection.tool}" + (f" ({detection.binary})" if detection.binary else "")]
        for controller in detection.controllers:
            volumes = ""
            topology = self.ctx.raid.topology(controller.index) if self.ctx.raid else None
            if topology is not None:
                volumes = f" VDs={len(topology.volumes)} free={len(topology.free_disk_ids)}"
            lines.append(
                f"  {controller.controller_id}: {controller.vendor.label} "
                f"{controller.model} FW={controller.firmware or '-'}{volumes}"
            )
        for attempt in detection.attempts:
            flag = "OK " if attempt.ok else "-- "
            lines.append(f"  probe {flag}{attempt.tool}: {attempt.detail or attempt.stderr[:60]}")
        self._detection.update("\n".join(lines))

    # -- workers ------------------------------------------------------------
    def _scan(self) -> None:
        """Run detection + enumeration + RAID topology off the UI thread."""

        result = run_scan(self.ctx)
        self.app.call_ui(self._after_scan, result.detection)

    def _after_scan(self, detection: DetectionResult) -> None:
        """Refresh the widgets once detection has finished."""

        self._render_detection(detection)
        self._render_summary()
        self.refresh()
        if not detection.ok:
            self.set_message("no controller tool answered - check vendor binaries in /usr/local/bin")
        else:
            volumes = sum(len(t.volumes) for t in (self.ctx.raid.topologies if self.ctx.raid else []))
            self.set_message(
                f"found {len(self.ctx.disks)} disks, {volumes} RAID volume(s) via {detection.tool}"
            )

    def _probe_network(self) -> None:
        """Wait for any usable IPv4 lease in the background (bounded)."""

        ready, _waited = wait_for_default_route(self.ctx.cfg.report.wait_for_route_seconds)
        self.ctx.network_ready = ready
        self.ctx.network_detail = network_summary() if ready else ""
        self.app.call_ui(self._render_summary)
        if ready and not self.ctx.shutdown_event.is_set():
            results = retry_pending(self.ctx.cfg)
            uploaded = sum(1 for result in results if result.ok)
            if uploaded:
                self.app.call_ui(lambda: self.set_message(f"flushed {uploaded} queued session(s)"))

    # -- events -------------------------------------------------------------
    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle the on-screen action buttons."""

        if event.button.id == "go-disks":
            self.go_disks()
        elif event.button.id == "raid":
            self.go_raid()
        elif event.button.id == "rescan":
            self.rescan()
        elif event.button.id == "upload":
            self.flush_pending()
        elif event.button.id == "quit":
            self.app.action_quit_app()

    def on_key(self, event) -> None:
        """Keyboard shortcuts that are not the focused widget's own keys."""

        key = getattr(event, "key", "")
        if key in ("r",):
            event.stop()
            self.rescan()
        elif key in ("c",):
            event.stop()
            self.go_raid()
        elif key in ("u",):
            event.stop()
            self.flush_pending()
        elif key in ("s",):
            event.stop()
            self.open_settings()

    # -- actions ------------------------------------------------------------
    def action_go_disks(self) -> None:
        """Enter with no focused button: keep the historical default."""

        self.go_disks()

    def action_go_raid(self) -> None:
        """C opens the RAID controller list."""

        self.go_raid()

    def go_disks(self) -> None:
        """Open the disk list screen."""

        from .disk_list import DiskListScreen

        self.app.push_screen(DiskListScreen(self.ctx))

    def go_raid(self) -> None:
        """Open the RAID controller list."""

        from .controllers import ControllersScreen

        self.app.push_screen(ControllersScreen(self.ctx))

    def open_settings(self) -> None:
        """Open the evidence-receiver settings screen."""

        from .settings import SettingsScreen

        self.app.push_screen(SettingsScreen(self.ctx))

    def on_screen_resume(self) -> None:
        """Re-render after returning from Settings (the receiver may have changed)."""

        self._render_summary()
        # The SSH probe finishes after the operator may have left Settings; the
        # verdict lives in ctx.last_message so it is not lost.
        if self.ctx.last_message:
            self.set_message(self.ctx.last_message)

    def rescan(self) -> None:
        """Re-run detection + enumeration."""

        self.set_message("rescanning...")
        self.ctx.spawn(self._scan, name="rescan")

    def flush_pending(self) -> None:
        """Retry queued evidence uploads.

        ``retry_pending`` waits up to 30s for a default route per session, so it
        must never run on the UI thread - that used to freeze the screen for
        minutes when the queue had a few entries.
        """

        self.set_message("retrying queued uploads...")

        def _worker() -> None:
            results = retry_pending(self.ctx.cfg)
            ok_count = sum(1 for result in results if result.ok)
            self.app.call_ui(
                lambda: self.set_message(f"upload attempts: {len(results)}, succeeded: {ok_count}")
            )

        self.ctx.spawn(_worker, name="flush-pending")
