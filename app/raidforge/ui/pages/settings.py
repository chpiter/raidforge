"""Settings: category list on the left, form on the right."""

from __future__ import annotations

import ipaddress
import os
import re
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Button, Input, Label, OptionList, Select, Static

from ... import config as config_mod
from ...core import report as report_mod
from ..models import HotkeyHint, PageId, RefreshScope
from ..palette import select_theme_name
from .base import StatefulPage

CATEGORIES = [
    ("Connection", "connection"),
    ("Reporting", "reporting"),
    ("Evidence", "evidence"),
    ("Appearance", "appearance"),
    ("About", "about"),
]

_HOST_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,252})$")


def _port_or(value: str, default: int) -> int:
    try:
        port = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return port if 1 <= port <= 65535 else default
_CONNECTION_INPUTS = {
    "settings-ssh-host",
    "settings-ssh-port",
    "settings-http-host",
    "settings-http-port",
    "settings-webplane-host",
    "settings-webplane-port",
}


class SettingsPage(StatefulPage):
    PAGE = PageId.SETTINGS
    SCOPES = frozenset({RefreshScope.SETTINGS})

    def __init__(self, state) -> None:
        super().__init__(state)
        self._transport = config_mod.normalize_transport(state.cfg.report.transport)
        self._checking = False
        self._error = Static("", classes="danger")
        self._endpoint = Static("", id="settings-endpoint", classes="muted")

    def compose(self) -> ComposeResult:
        yield OptionList(
            *[label for label, _key in CATEGORIES],
            id="settings-categories",
        )
        yield VerticalScroll(id="settings-form")

    def on_mount(self) -> None:
        super().on_mount()
        self.call_after_refresh(self._show, "connection")

    def hotkeys(self) -> List[HotkeyHint]:
        hints = [
            HotkeyHint("Enter", "save"),
            HotkeyHint("Tab", "field"),
            HotkeyHint("F5", "refresh"),
            HotkeyHint("Esc", "back"),
            HotkeyHint("Ctrl+Q", "quit"),
        ]
        try:
            width = self.app.size.width
        except Exception:  # pragma: no cover - not mounted yet
            width = 80
        if width >= 100:
            hints.append(HotkeyHint("Ctrl+P", "commands"))
        return hints

    def focus_primary(self) -> None:
        try:
            self.query_one("#settings-categories", OptionList).focus()
        except Exception:  # pragma: no cover
            pass

    def refresh_data(self) -> None:
        self.refresh_chrome()

    # -- form switching -----------------------------------------------------
    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        label = str(event.option.prompt)
        key = next((key for text, key in CATEGORIES if text == label), "connection")
        self.call_after_refresh(self._show, key)

    async def _show(self, key: str) -> None:
        form = self.query_one("#settings-form", VerticalScroll)
        await form.remove_children()
        if key == "connection":
            await self._show_connection(form)
        elif key == "reporting":
            await self._show_reporting(form)
        elif key == "evidence":
            await self._show_evidence(form)
        elif key == "appearance":
            await self._show_appearance(form)
        else:
            await self._show_about(form)

    async def _show_connection(self, form: VerticalScroll) -> None:
        cfg = self.state.cfg
        report = cfg.report
        self._transport = config_mod.normalize_transport(report.transport)
        transport = Select(
            [("SSH", "ssh"), ("HTTP", "http")],
            value=self._transport,
            allow_blank=False,
            id="settings-transport",
        )
        ssh_host = Input(
            value=report.ssh_host, placeholder="192.168.40.129", id="settings-ssh-host"
        )
        ssh_port = Input(
            value=str(report.ssh_port), placeholder="22", id="settings-ssh-port", classes="port"
        )
        http_host = Input(
            value=report.http_host, placeholder="empty = webplane", id="settings-http-host"
        )
        http_port = Input(
            value=str(report.http_port), placeholder="8080", id="settings-http-port", classes="port"
        )
        web_host, web_port, web_scheme, source = self._webplane_defaults()
        webplane_host = Input(
            value=web_host, placeholder="192.168.40.132", id="settings-webplane-host"
        )
        webplane_port = Input(
            value=str(web_port),
            placeholder="8080",
            id="settings-webplane-port",
            classes="port",
        )
        scheme = Select(
            [("HTTP", "http"), ("HTTPS", "https")],
            value=web_scheme,
            allow_blank=False,
            id="settings-webplane-scheme",
            classes="scheme",
        )
        fixed = Static(
            f"User: {report.ssh_user}   Remote root: {report.ssh_remote_root}",
            classes="muted",
        )
        source_line = Static(
            f"api prefix: {cfg.webplane.api_prefix}   source: {source}", classes="muted"
        )
        note = Static(
            "Saved to config.d/90-site.yaml and /run/raidforge/agent.env for this boot; "
            "HTTP host empty = follow webplane.",
            classes="muted",
        )
        actions = Horizontal(
            Button("Save [Enter]", id="settings-save"),
            Button("Probe", id="settings-probe"),
            id="settings-actions",
            classes="form-actions",
        )
        await form.mount(
            Label("[b]Connection[/b] — evidence upload & control plane", classes="title"),
            fixed,
            Label("[b]Preferred evidence transport[/b] (the other channel remains fallback)"),
            transport,
            Label("[b]SSH evidence receiver[/b] (IPv4 / port)"),
            Horizontal(ssh_host, ssh_port, classes="settings-row"),
            Label("[b]HTTP evidence receiver[/b] (host / port; empty host = webplane)"),
            Horizontal(http_host, http_port, classes="settings-row"),
            Label("[b]Webplane control plane[/b] (host / port / scheme)"),
            Horizontal(webplane_host, webplane_port, scheme, classes="settings-row"),
            source_line,
            self._endpoint,
            self._error,
            note,
            actions,
        )
        transport.value = self._transport
        self._update_endpoint_preview()

    async def _show_reporting(self, form: VerticalScroll) -> None:
        cfg = self.state.cfg
        report = cfg.report
        endpoint = report_mod.http_endpoint(cfg) or "-"
        webplane = report_mod.webplane_url(cfg) or "-"
        await form.mount(
            Label("[b]Reporting[/b]", classes="title"),
            Static(f"enabled            : {report.enabled}"),
            Static(f"preferred transport: {report.transport}"),
            Static(f"ssh endpoint       : {report.ssh_host or '-'}:{report.ssh_port}"),
            Static(f"http endpoint      : {endpoint}"),
            Static(f"webplane           : {webplane}{cfg.webplane.api_prefix}"),
            Static(f"run upload budget  : {report.run_upload_seconds:.0f}s"),
            Static(f"pending queue      : {report.pending_root}"),
            Static("These values are read-only here; edit config.yaml for factory defaults.", classes="muted"),
        )

    async def _show_evidence(self, form: VerticalScroll) -> None:
        evidence = self.state.cfg.evidence
        await form.mount(
            Label("[b]Evidence[/b]", classes="title"),
            Static(f"root                 : {evidence.root}"),
            Static(f"record session       : {evidence.record_session}"),
            Static(f"recorder             : {evidence.recorder}"),
            Static(f"snapshot on screen   : {evidence.snapshot_on_screen_change}"),
            Static(f"current session      : {self.state.evidence.session_id}"),
            Static(f"manifest             : {self.state.evidence.manifest_path}"),
            Static("events.jsonl is append-only and fsynced by the existing evidence layer.", classes="muted"),
        )

    async def _show_appearance(self, form: VerticalScroll) -> None:
        current = self.state.cfg.ui.theme or "raidforge"
        theme = Select(
            [
                ("Auto (deep blue)", "auto"),
                ("Deep blue 7.2", "raidforge"),
                ("Mono (16 colour console)", "raidforge-mono"),
            ],
            value=current,
            allow_blank=False,
            id="settings-theme",
        )
        await form.mount(
            Label("[b]Appearance[/b]", classes="title"),
            Label("[b]Theme[/b]"),
            theme,
            Static(
                "Auto always uses the deep-blue palette. Choose Mono only when the "
                "16-colour VGA/fbcon console makes dark backgrounds invisible.",
                classes="muted",
            ),
            Static(f"active theme: {self.app.theme}", id="settings-active-theme", classes="muted"),
            Static("Density: compact 80x25 (fixed).", classes="muted"),
        )

    async def _show_about(self, form: VerticalScroll) -> None:
        await form.mount(
            Label("[b]About[/b]", classes="title"),
            Static(f"version : {self.state.cfg.app_version}"),
            Static(f"server  : {self.state.server_sn}"),
            Static(f"session : {self.state.evidence.session_id}"),
            Static(f"evidence: {self.state.evidence.root}"),
            Static(f"python  : {os.sys.version.split()[0]}"),
            Static(f"dry-run : {self.state.cfg.dry_run}"),
        )

    # -- connection helpers -------------------------------------------------
    def _webplane_defaults(self) -> tuple[str, int, str, str]:
        cfg = self.state.cfg
        web = cfg.webplane
        resolved = report_mod.webplane_url(cfg)
        parsed = urlsplit(resolved) if resolved else None
        if web.host:
            return web.host, web.port, web.scheme, "90-site.yaml"
        host = (parsed.hostname if parsed else "") or ""
        port = (parsed.port if parsed and parsed.port else web.port) or 8080
        scheme = (
            parsed.scheme
            if parsed and parsed.scheme in {"http", "https"}
            else web.scheme
        ) or "http"
        return host, port, scheme, "agent.yaml / default"

    def _raw_values(self) -> Optional[Dict[str, Any]]:
        try:
            return {
                "transport": str(self.query_one("#settings-transport", Select).value),
                "ssh_host": self.query_one("#settings-ssh-host", Input).value.strip(),
                "ssh_port": self.query_one("#settings-ssh-port", Input).value.strip(),
                "http_host": self.query_one("#settings-http-host", Input).value.strip(),
                "http_port": self.query_one("#settings-http-port", Input).value.strip(),
                "webplane_host": self.query_one("#settings-webplane-host", Input).value.strip(),
                "webplane_port": self.query_one("#settings-webplane-port", Input).value.strip(),
                "webplane_scheme": str(
                    self.query_one("#settings-webplane-scheme", Select).value
                ),
            }
        except Exception:  # pragma: no cover - form not mounted
            return None

    def _read_form(self) -> Optional[Dict[str, Any]]:
        raw = self._raw_values()
        if raw is None:
            return None
        ssh_host = raw["ssh_host"]
        if ssh_host:
            try:
                ipaddress.IPv4Address(ssh_host)
            except ValueError:
                self._error.update("SSH receiver must be a valid IPv4 address")
                self.state.evidence.emit(
                    "settings_rejected", ssh_host=ssh_host, reason="bad_ipv4"
                )
                return None
        for label, value in (("HTTP", raw["http_host"]), ("webplane", raw["webplane_host"])):
            if value and not _HOST_RE.match(value):
                self._error.update(f"{label} host is not a valid hostname or IPv4 address")
                self.state.evidence.emit(
                    "settings_rejected", reason="bad_host", host=value
                )
                return None
        ports: Dict[str, int] = {}
        for label, key, default in (
            ("SSH port", "ssh_port", 22),
            ("HTTP port", "http_port", 8080),
            ("webplane port", "webplane_port", 8080),
        ):
            text = raw[key]
            try:
                value = int(text) if text else default
            except ValueError:
                value = 0
            if not 1 <= value <= 65535:
                self._error.update(f"{label} must be between 1 and 65535")
                self.state.evidence.emit("settings_rejected", reason="bad_port")
                return None
            ports[key] = value
        return {
            "transport": config_mod.normalize_transport(raw["transport"]),
            "ssh_host": ssh_host,
            "ssh_port": ports["ssh_port"],
            "http_host": raw["http_host"],
            "http_port": ports["http_port"],
            "webplane_host": raw["webplane_host"],
            "webplane_port": ports["webplane_port"],
            "webplane_scheme": raw["webplane_scheme"],
        }

    def _preview_endpoint(self, values: Dict[str, Any]) -> str:
        report = config_mod.ReportConfig(
            transport=values["transport"],
            ssh_host=values["ssh_host"],
            ssh_port=values["ssh_port"],
            http_host=values["http_host"],
            http_port=values["http_port"],
            http_base_url=self.state.cfg.report.http_base_url,
        )
        if values["webplane_host"]:
            webplane = report_mod.http_url_for(
                values["webplane_host"],
                values["webplane_port"],
                values["webplane_scheme"],
            )
        else:
            webplane = report_mod.webplane_url(self.state.cfg)
        return report_mod.http_endpoint_for(report, webplane)

    def _update_endpoint_preview(self) -> None:
        raw = self._raw_values()
        if raw is None:
            return
        values = {
            "transport": config_mod.normalize_transport(raw["transport"]),
            "ssh_host": raw["ssh_host"],
            "ssh_port": _port_or(raw["ssh_port"], 22),
            "http_host": raw["http_host"],
            "http_port": _port_or(raw["http_port"], 8080),
            "webplane_host": raw["webplane_host"],
            "webplane_port": _port_or(raw["webplane_port"], 8080),
            "webplane_scheme": raw["webplane_scheme"],
        }
        endpoint = self._preview_endpoint(values)
        if endpoint:
            self._endpoint.update(f"HTTP endpoint: {endpoint}/sessions")
        else:
            self._endpoint.update(
                "HTTP endpoint: disabled (set an HTTP host or use HTTP transport)"
            )

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "settings-transport":
            self._transport = str(event.value)
            self._update_endpoint_preview()
        elif event.select.id == "settings-webplane-scheme":
            self._update_endpoint_preview()
        elif event.select.id == "settings-theme":
            choice = str(event.value)
            self.state.cfg.ui.theme = choice
            try:
                config_mod.write_site_override({"ui": {"theme": choice}})
            except Exception as exc:  # pragma: no cover - read-only /etc
                self.toast(f"theme applied for this session only: {exc}", "warn")
            selected = select_theme_name(choice)
            self.app.theme = selected
            self.state.evidence.emit("ui_theme_changed", theme=selected, configured=choice)
            try:
                self.app.screen._update_chrome()  # type: ignore[attr-defined]
            except Exception:  # pragma: no cover
                pass
            self.toast(f"theme: {selected}")

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id in _CONNECTION_INPUTS:
            self._update_endpoint_preview()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id in _CONNECTION_INPUTS:
            self._save_connection()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "settings-save":
            self._save_connection()
        elif event.button.id == "settings-probe":
            self._probe()

    def _save_connection(self) -> None:
        values = self._read_form()
        if values is None:
            return
        path = self._apply_form(values, persist=True, reconnect=True)
        if path is None:
            return
        self._error.update("")
        self.toast(f"saved to {path}; probing {values['transport'].upper()}")
        self._probe_values(values)

    def _apply_form(
        self, values: Dict[str, Any], *, persist: bool, reconnect: bool
    ) -> Optional[Any]:
        from ...agent.config import write_run_env

        cfg = self.state.cfg
        updates = {
            "report": {
                "transport": values["transport"],
                "ssh_host": values["ssh_host"],
                "ssh_port": values["ssh_port"],
                "http_host": values["http_host"],
                "http_port": values["http_port"],
                "http_base_url": "",
            },
            "webplane": {
                "host": values["webplane_host"],
                "port": values["webplane_port"],
                "scheme": values["webplane_scheme"],
            },
        }
        path = None
        if persist:
            try:
                path = config_mod.write_site_override(updates)
            except (OSError, RuntimeError) as exc:
                self._error.update(f"cannot write {config_mod.SITE_OVERRIDE_PATH}: {exc}")
                return None
        report = cfg.report
        report.transport = values["transport"]
        report.ssh_host = values["ssh_host"]
        report.ssh_port = values["ssh_port"]
        report.http_host = values["http_host"]
        report.http_port = values["http_port"]
        report.http_base_url = ""
        web = cfg.webplane
        web.host = values["webplane_host"]
        web.port = values["webplane_port"]
        web.scheme = values["webplane_scheme"]
        new_webplane = report_mod.webplane_url(cfg)
        prefix = web.api_prefix
        if persist:
            try:
                if values["webplane_host"]:
                    write_run_env(
                        {"SERVER_URL": new_webplane, "API_PREFIX": prefix}
                    )
                else:
                    write_run_env({}, remove=("SERVER_URL", "API_PREFIX"))
            except OSError as exc:  # pragma: no cover - read-only /run
                self.toast(f"agent.env not written: {exc}", "warn")
            if reconnect and new_webplane:
                runtime = getattr(self.state, "agent_runtime", None)
                if runtime is not None and hasattr(runtime, "request_webplane"):
                    try:
                        applied = runtime.request_webplane(new_webplane, prefix)
                    except Exception as exc:
                        self._error.update(f"webplane reconnect failed: {exc}")
                    else:
                        if not applied:
                            self.toast(
                                "webplane change will apply when the current task finishes",
                                "warn",
                            )
        self.state.evidence.emit(
            "settings_changed",
            transport=values["transport"],
            ssh_host=values["ssh_host"],
            ssh_port=values["ssh_port"],
            http_host=values["http_host"],
            http_port=values["http_port"],
            webplane_host=values["webplane_host"],
            webplane_port=values["webplane_port"],
            webplane_scheme=values["webplane_scheme"],
            http_endpoint=report_mod.http_endpoint(cfg),
            path=str(path) if path is not None else "",
        )
        return path

    def _probe_values(self, values: Dict[str, Any]) -> None:
        if values["transport"] == "http":
            endpoint = report_mod.http_endpoint(self.state.cfg)
            if not endpoint:
                self.toast("no HTTP receiver configured")
                return
            self._start_probe("http", endpoint, values["http_port"], endpoint=endpoint)
            return
        if not values["ssh_host"]:
            self.toast("enter an SSH receiver IP first")
            return
        self._start_probe("ssh", values["ssh_host"], values["ssh_port"])

    def _probe(self) -> None:
        values = self._read_form()
        if values is None:
            return
        self._apply_form(values, persist=False, reconnect=False)
        self._probe_values(values)

    def _start_probe(self, mode: str, host: str, port: int, endpoint: str = "") -> None:
        if self._checking:
            self.toast("a receiver check is already running")
            return
        self._checking = True
        cfg = self.state.cfg
        self.state.evidence.emit(
            "settings_http_check_started" if mode == "http" else "settings_ssh_check_started",
            ssh_host=host,
            ssh_port=port,
            endpoint=endpoint,
        )

        def worker() -> None:
            try:
                if mode == "http":
                    result = report_mod.check_http(cfg, timeout=5.0, endpoint=endpoint)
                else:
                    result = report_mod.check_ssh(cfg, timeout=5.0)
            except Exception as exc:  # pragma: no cover - probes are defensive
                result = report_mod.ProbeCheck(False, f"{mode} check crashed: {exc}")
            self.app.call_ui(self._after_probe, result, mode, host, port)

        self.state.spawn(worker, name=f"{mode}-check")

    def _after_probe(self, result, mode: str, host: str, port: int) -> None:
        self._checking = False
        ok = bool(result.ok)
        self.state.evidence.emit(
            "settings_http_check" if mode == "http" else "settings_ssh_check",
            ssh_host=host,
            ssh_port=port,
            ok=ok,
            message=result.message,
            detail=(result.detail or "")[-2000:],
        )
        if ok:
            self._error.update("")
            self.toast(f"receiver {host} reachable via {mode.upper()}")
        else:
            self._error.update(f"{mode.upper()} check failed; the other channel will be tried")
            self.toast(f"{mode.upper()} check failed: {result.message}", "warn")
        try:
            self.app.bell()
        except Exception:  # pragma: no cover
            pass


__all__ = ["SettingsPage"]
