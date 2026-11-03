"""Evidence-receiver settings: transport, address and port.

The operator chooses the preferred upload channel (SSH or HTTP) and edits one
receiver IPv4 plus the port for that channel.  The other channel remains a
fallback; local queueing is the last resort.  SSH identity details (user, key
path, remote root) stay fixed in the image: the key is baked into the squashfs
and the collector account is created by the site.

The saved values go to ``/etc/raidforge/config.d/90-site.yaml``, a drop-in that
is merged over the factory ``config.yaml`` (see :mod:`raidforge.config`).  Live
/etc is a tmpfs overlay, so a change lasts for the current boot; the permanent
default is baked into the image.

Saving is deliberately *save-then-probe*: values are persisted first, then the
selected channel is tried in the background.  A failed probe keeps the
configuration and says so loudly - rolling back would punish a momentary
network hiccup and lose the address the operator just read off the collector.
"""

from __future__ import annotations

import ipaddress
import os
from typing import Iterable
from urllib.parse import urlsplit

from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.widgets import Button, Input, Label, Static

from ... import config as config_mod
from ...core import report as report_mod
from ..state import AppContext
from .base import BaseScreen

SCREEN_NAME = "Settings"

#: Kept short: the operator is standing at a console waiting for feedback.
SSH_CHECK_TIMEOUT = 5.0
HTTP_CHECK_TIMEOUT = SSH_CHECK_TIMEOUT

_ENV_WATCH = (
    "RAIDFORGE_REPORT_TRANSPORT",
    "RAIDFORGE_REPORT_SSH_HOST",
    "RAIDFORGE_REPORT_SSH_PORT",
    "RAIDFORGE_REPORT_HTTP_PORT",
    "RAIDFORGE_REPORT_HTTP_BASE_URL",
)


class SettingsScreen(BaseScreen):
    """Receiver transport/address/port plus the fixed SSH identity."""

    SCREEN_NAME = SCREEN_NAME
    HINTS = [
        ("Enter", "save"),
        ("Tab", "field"),
        ("Esc", "back"),
        ("Q", "quit"),
    ]

    def __init__(self, ctx: AppContext) -> None:
        super().__init__(ctx)
        self._checking = False
        self._transport = config_mod.normalize_transport(ctx.cfg.report.transport)
        self._ports = {"ssh": ctx.cfg.report.ssh_port, "http": ctx.cfg.report.http_port}
        #: Preserve an explicit https:// base and its path prefix on save.
        self._scheme = "http"
        self._http_path = ""
        self._apply_explicit_http_base(ctx.cfg.report.http_base_url)

        self._fixed = Static("", classes="muted")
        self._error = Static("", classes="danger")
        self._note = Static("", classes="muted")
        self._endpoint = Static("", id="http-endpoint", classes="muted")
        self._ssh_host = Input(
            value=ctx.cfg.report.ssh_host,
            placeholder="192.168.40.129",
            id="ssh-host",
        )
        self._port = Input(
            value=str(self._ports[self._transport]),
            placeholder="22" if self._transport == "ssh" else "8080",
            id="port",
        )
        self._ssh_mode = Button("", id="transport-ssh", classes="seg")
        self._http_mode = Button("", id="transport-http", classes="seg")
        self._save = Button("Save [Enter]", id="save")
        self._back = Button("Back [Esc]", id="back")
        self._refresh_transport_buttons()

    # -- helpers ------------------------------------------------------------
    def _apply_explicit_http_base(self, base: str) -> None:
        """Adopt scheme, path and port from an explicit ``http_base_url``."""

        text = (base or "").strip()
        if not text:
            return
        if "://" not in text:
            text = "http://" + text
        try:
            parts = urlsplit(text)
        except ValueError:  # pragma: no cover - malformed explicit URL
            return
        if parts.scheme in ("http", "https"):
            self._scheme = parts.scheme
        if parts.path and parts.path != "/":
            self._http_path = parts.path.rstrip("/")
        try:
            if parts.port:
                self._ports["http"] = parts.port
            elif parts.scheme == "https":
                self._ports["http"] = 443
            elif parts.scheme == "http":
                self._ports["http"] = 80
        except ValueError:  # pragma: no cover - malformed explicit URL
            pass

    def _refresh_transport_buttons(self) -> None:
        self._ssh_mode.label = "[X] SSH" if self._transport == "ssh" else "[ ] SSH"
        self._http_mode.label = "[X] HTTP" if self._transport == "http" else "[ ] HTTP"

    def _base_url(self, host: str, port: int) -> str:
        return report_mod.http_url_for(host, port, self._scheme, self._http_path)

    def _buffer_port(self) -> None:
        """Remember the current port for the active transport if it is valid."""

        text = self._port.value.strip()
        if text.isdigit():
            value = int(text)
            if 1 <= value <= 65535:
                self._ports[self._transport] = value

    def _update_endpoint(self) -> None:
        host = self._ssh_host.value.strip()
        if not host:
            self._endpoint.update("HTTP endpoint: disabled (no receiver IP)")
            return
        port = self._ports["http"]
        if self._transport == "http":
            text = self._port.value.strip()
            if text.isdigit() and 1 <= int(text) <= 65535:
                port = int(text)
        url = self._base_url(host, port)
        self._endpoint.update(f"HTTP endpoint: {url or '-'}")

    def _note_text(self) -> str:
        present = [name for name in _ENV_WATCH if os.environ.get(name)]
        if present:
            text = "!! " + ", ".join(present) + " set; environment overrides this screen."
        else:
            text = (
                "Saved to config.d/90-site.yaml until reboot; "
                "empty = queue locally."
            )
        explicit = (self.ctx.cfg.report.http_base_url or "").strip()
        if explicit:
            derived = self._base_url(
                self.ctx.cfg.report.ssh_host, self._ports["http"]
            ).rstrip("/")
            if derived and explicit.rstrip("/") != derived:
                text += f"\nExisting http_base_url {explicit} will be replaced on Save."
        return text

    def _set_busy(self, busy: bool) -> None:
        for widget in (
            self._save,
            self._ssh_mode,
            self._http_mode,
            self._ssh_host,
            self._port,
        ):
            try:
                widget.disabled = busy
            except Exception:  # pragma: no cover - screen may be gone
                pass

    # -- layout -------------------------------------------------------------
    def build_content(self) -> Iterable[object]:
        report = self.ctx.cfg.report
        self._fixed.update(
            f"User: {report.ssh_user}   Remote root: {report.ssh_remote_root}"
        )
        self._note.update(self._note_text())
        self._update_endpoint()
        yield Label(f"[b]{SCREEN_NAME}[/b] - evidence receiver", classes="title")
        yield self._fixed
        yield Label("[b]Transport[/b] (selected first, the other is the fallback)")
        with Horizontal(id="transport-row"):
            yield self._ssh_mode
            yield self._http_mode
        yield Label("[b]Receiver IPv4 / port[/b]")
        with Horizontal(id="host-row"):
            yield self._ssh_host
            yield self._port
        yield self._endpoint
        yield self._error
        yield self._note
        with Horizontal(id="settings-actions"):
            yield self._save
            yield self._back
        yield self.status_line()

    def on_mount(self) -> None:
        """Focus the address field so the operator can type immediately."""

        self._ssh_host.focus()
        self._update_endpoint()
        self.schedule_snapshot("08-settings")

    # -- events -------------------------------------------------------------
    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Enter inside either input saves."""

        event.stop()
        self._save_values()

    def on_input_changed(self, event: Input.Changed) -> None:
        if getattr(getattr(event, "input", None), "id", "") in ("ssh-host", "port"):
            self._update_endpoint()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "save":
            self._save_values()
        elif event.button.id == "back":
            self.app.pop_screen()
        elif event.button.id == "transport-ssh":
            self._switch_transport("ssh")
        elif event.button.id == "transport-http":
            self._switch_transport("http")

    def on_key(self, event) -> None:
        if getattr(event, "key", "") == "escape":
            event.stop()
            self.app.pop_screen()

    # -- transport selection ------------------------------------------------
    def _switch_transport(self, target: str) -> None:
        if target not in ("ssh", "http"):
            return
        self._buffer_port()
        if target == self._transport:
            self._port.focus()
            return
        self._transport = target
        self._refresh_transport_buttons()
        self._port.value = str(self._ports[target])
        self._port.placeholder = "22" if target == "ssh" else "8080"
        self._error.update("")
        self._update_endpoint()
        self._port.focus()

    # -- saving -------------------------------------------------------------
    def _save_values(self) -> None:
        if self._checking:
            # Enter is easy to hit twice; a second probe would only stack
            # timeouts behind the first one.
            self.set_message("a receiver check is already running - wait for the result")
            return

        host = self._ssh_host.value.strip()
        if host:
            try:
                ipaddress.IPv4Address(host)
            except ValueError:
                self._error.update(
                    "not a valid IPv4 address - enter four octets, e.g. 192.168.40.129"
                )
                self.ctx.evidence.emit(
                    "settings_rejected", ssh_host=host, reason="bad_ipv4"
                )
                self._ssh_host.focus()
                return

        port_text = self._port.value.strip()
        try:
            port = int(port_text)
        except ValueError:
            port = 0
        if not 1 <= port <= 65535:
            self._error.update("port must be a whole number between 1 and 65535")
            self.ctx.evidence.emit(
                "settings_rejected",
                ssh_host=host,
                transport=self._transport,
                port=port_text,
                reason="bad_port",
            )
            self._port.focus()
            return
        self._ports[self._transport] = port

        http_port = self._ports["http"]
        http_base = "" if not host else self._base_url(host, http_port)
        updates = {
            "report": {
                "transport": self._transport,
                "ssh_host": host,
                "ssh_port": self._ports["ssh"],
                "http_port": http_port,
                "http_base_url": http_base,
            }
        }
        try:
            path = config_mod.write_site_override(updates)
        except (OSError, RuntimeError) as exc:
            self._error.update(f"cannot write {config_mod.SITE_OVERRIDE_PATH}: {exc}")
            return

        # report.py reads cfg.report at call time, so the post-exit upload and
        # the pending-queue retries pick this up without a restart.
        self.ctx.cfg.report.transport = self._transport
        self.ctx.cfg.report.ssh_host = host
        self.ctx.cfg.report.ssh_port = self._ports["ssh"]
        self.ctx.cfg.report.http_port = http_port
        self.ctx.cfg.report.http_base_url = http_base
        self._error.update("")
        self.ctx.evidence.emit(
            "settings_changed",
            ssh_host=host,
            transport=self._transport,
            ssh_port=self._ports["ssh"],
            http_port=http_port,
            http_base_url=http_base,
            path=str(path),
        )

        if not host:
            self.set_message(f"receiver disabled - evidence will queue locally ({path})")
            self.app.bell()
            return

        self._start_check(host)

    # -- probing ------------------------------------------------------------
    def _start_check(self, host: str) -> None:
        """Probe the just-saved preferred channel without blocking the console."""

        self._checking = True
        self._set_busy(True)
        transport = self._transport
        port = self._ports[transport]
        cfg = self.ctx.cfg

        if transport == "http":
            endpoint = report_mod.http_endpoint(cfg)
            self.set_message(f"checking HTTP {endpoint}/healthz ...")
            self.ctx.evidence.emit("settings_http_check_started", endpoint=endpoint)
            checker = report_mod.check_http
            timeout = HTTP_CHECK_TIMEOUT
        else:
            self.set_message(f"checking SSH to {host}:{port} ...")
            self.ctx.evidence.emit(
                "settings_ssh_check_started", ssh_host=host, ssh_port=port
            )
            checker = report_mod.check_ssh
            timeout = SSH_CHECK_TIMEOUT

        def worker() -> None:
            try:
                result = checker(cfg, timeout=timeout)
            except Exception as exc:  # pragma: no cover - probe must never kill the app
                result = report_mod.ProbeCheck(
                    False, f"{transport} check crashed: {exc}"
                )
            self.app.call_ui(self._after_check, result, host, transport, port)

        self.ctx.spawn(worker, name=f"{transport}-check")

    def _after_check(
        self,
        result: "report_mod.ProbeCheck",
        host: str,
        transport: str,
        port: int,
    ) -> None:
        """Show the probe result; runs on the UI thread."""

        self._checking = False
        self._set_busy(False)

        try:
            if transport == "http":
                self.ctx.evidence.emit(
                    "settings_http_check",
                    endpoint=report_mod.http_endpoint(self.ctx.cfg),
                    ok=bool(result.ok),
                    message=result.message,
                    detail=(result.detail or "")[-2000:],
                )
            else:
                self.ctx.evidence.emit(
                    "settings_ssh_check",
                    ssh_host=host,
                    ssh_port=port,
                    ok=bool(result.ok),
                    message=result.message,
                    detail=(result.detail or "")[-2000:],
                )
        except Exception:  # pragma: no cover - session may already be finalised
            pass

        if result.ok:
            message = (
                f"receiver {host} reachable - evidence will try "
                f"{transport.upper()} first"
            )
            try:
                self._error.update("")
            except Exception:  # pragma: no cover
                pass
        else:
            other = "HTTP" if transport == "ssh" else "SSH"
            message = (
                f"saved, but {transport.upper()} check failed: {result.message} - "
                f"will try {other}, then queue locally"
            )
            try:
                self._error.update(message)
            except Exception:  # pragma: no cover
                pass

        # set_message also stores ctx.last_message, so a Dashboard that the
        # operator switched back to still shows the verdict on resume.
        self.set_message(message)
        try:
            self.app.bell()
        except Exception:  # pragma: no cover - app shutting down
            pass
