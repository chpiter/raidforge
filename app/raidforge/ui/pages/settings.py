"""Settings: category list on the left, form on the right."""

from __future__ import annotations

import ipaddress
import os
from typing import List

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


class SettingsPage(StatefulPage):
    PAGE = PageId.SETTINGS
    SCOPES = frozenset({RefreshScope.SETTINGS})

    def __init__(self, state) -> None:
        super().__init__(state)
        self._transport = config_mod.normalize_transport(state.cfg.report.transport)
        self._ports = {"ssh": state.cfg.report.ssh_port, "http": state.cfg.report.http_port}
        self._checking = False
        self._error = Static("", classes="danger")

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
        port = self._ports.get(self._transport, report.ssh_port)
        transport = Select(
            [("SSH", "ssh"), ("HTTP", "http")],
            value=self._transport,
            allow_blank=False,
            id="settings-transport",
        )
        host = Input(value=report.ssh_host, placeholder="192.168.40.129", id="settings-host")
        port_input = Input(value=str(port), placeholder="22", id="settings-port")
        endpoint = Static("", id="settings-endpoint", classes="muted")
        fixed = Static(
            f"User: {report.ssh_user}   Remote root: {report.ssh_remote_root}",
            classes="muted",
        )
        note = Static(
            "Saved to config.d/90-site.yaml until reboot; empty = queue locally.",
            classes="muted",
        )
        actions = Horizontal(
            Button("Save [Enter]", id="settings-save"),
            Button("Probe", id="settings-probe"),
            id="settings-actions",
            classes="form-actions",
        )
        await form.mount(
            Label("[b]Connection[/b] — preferred upload channel", classes="title"),
            fixed,
            Label("[b]Transport[/b] (the other channel remains the fallback)"),
            transport,
            Label("[b]Receiver IPv4 / port[/b]"),
            host,
            port_input,
            endpoint,
            self._error,
            note,
            actions,
        )
        transport.value = self._transport
        self._update_endpoint(host, port_input, endpoint, transport)

    async def _show_reporting(self, form: VerticalScroll) -> None:
        report = self.state.cfg.report
        await form.mount(
            Label("[b]Reporting[/b]", classes="title"),
            Static(f"enabled            : {report.enabled}"),
            Static(f"preferred transport: {report.transport}"),
            Static(f"ssh endpoint       : {report.ssh_host or '-'}:{report.ssh_port}"),
            Static(f"http endpoint      : {report.http_base_url or '-'}"),
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
    def _base_url(self, host: str, port: int) -> str:
        return report_mod.http_url_for(host, port, "http", "")

    def _update_endpoint(self, host: Input, port: Input, endpoint: Static, transport: Select) -> None:
        value = host.value.strip()
        if not value:
            endpoint.update("HTTP endpoint: disabled (no receiver IP)")
            return
        mode = str(transport.value)
        port_value = self._ports["http"]
        if mode == "http":
            text = port.value.strip()
            if text.isdigit() and 1 <= int(text) <= 65535:
                port_value = int(text)
        endpoint.update(f"HTTP endpoint: {self._base_url(value, port_value)}")

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "settings-transport":
            self._transport = str(event.value)
            try:
                port = self.query_one("#settings-port", Input)
                port.value = str(self._ports.get(self._transport, 22))
            except Exception:  # pragma: no cover
                pass
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

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id in ("settings-host", "settings-port"):
            self._save_connection()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "settings-save":
            self._save_connection()
        elif event.button.id == "settings-probe":
            self._probe()

    def _save_connection(self) -> None:
        try:
            host = self.query_one("#settings-host", Input).value.strip()
            port_input = self.query_one("#settings-port", Input)
            transport = self.query_one("#settings-transport", Select)
        except Exception:  # pragma: no cover - form not mounted
            return
        if host:
            try:
                ipaddress.IPv4Address(host)
            except ValueError:
                self._error.update("not a valid IPv4 address")
                self.state.evidence.emit("settings_rejected", ssh_host=host, reason="bad_ipv4")
                return
        text = port_input.value.strip()
        try:
            port = int(text)
        except ValueError:
            port = 0
        if not 1 <= port <= 65535:
            self._error.update("port must be between 1 and 65535")
            self.state.evidence.emit("settings_rejected", ssh_host=host, reason="bad_port")
            return
        mode = str(transport.value)
        self._ports[mode] = port
        http_base = "" if not host else self._base_url(host, self._ports["http"])
        updates = {
            "report": {
                "transport": mode,
                "ssh_host": host,
                "ssh_port": self._ports["ssh"],
                "http_port": self._ports["http"],
                "http_base_url": http_base,
            }
        }
        try:
            path = config_mod.write_site_override(updates)
        except (OSError, RuntimeError) as exc:
            self._error.update(f"cannot write {config_mod.SITE_OVERRIDE_PATH}: {exc}")
            return
        report = self.state.cfg.report
        report.transport = mode
        report.ssh_host = host
        report.ssh_port = self._ports["ssh"]
        report.http_port = self._ports["http"]
        report.http_base_url = http_base
        self._error.update("")
        self.state.evidence.emit(
            "settings_changed",
            ssh_host=host,
            transport=mode,
            ssh_port=self._ports["ssh"],
            http_port=self._ports["http"],
            http_base_url=http_base,
            path=str(path),
        )
        self.toast(f"saved to {path}; probing {mode.upper()}")
        if host:
            self._start_probe(mode, host, port)

    def _probe(self) -> None:
        try:
            host = self.query_one("#settings-host", Input).value.strip()
            port_input = self.query_one("#settings-port", Input)
            mode = str(self.query_one("#settings-transport", Select).value)
        except Exception:  # pragma: no cover
            return
        if not host:
            self.toast("enter a receiver IP first")
            return
        self._start_probe(mode, host, int(port_input.value or 0))

    def _start_probe(self, mode: str, host: str, port: int) -> None:
        if self._checking:
            self.toast("a receiver check is already running")
            return
        self._checking = True
        cfg = self.state.cfg
        self.state.evidence.emit(
            "settings_http_check_started" if mode == "http" else "settings_ssh_check_started",
            ssh_host=host,
            ssh_port=port,
        )

        def worker() -> None:
            try:
                if mode == "http":
                    result = report_mod.check_http(cfg, timeout=5.0)
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
