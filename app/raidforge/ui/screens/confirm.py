"""Unified confirmation gate for erase, create, delete and clear-config.

The acknowledgement word remains the short, deliberate ``yes`` (or the
controller id for clear-config), as documented in the README.  The full
``CONFIRM-{server_sn}`` token is shown as an audit identifier but is not typed.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Static

from ..focus import capture_focus
from ..models import ConfirmRequest
from ..state import AppState


class ConfirmModal(ModalScreen[bool]):
    BINDINGS = [Binding("escape", "cancel", "Cancel", priority=True)]

    def __init__(self, state: AppState, request: ConfirmRequest) -> None:
        super().__init__()
        self.state = state
        self.request = request
        self._error = Static("", id="confirm-error")
        self._ack = Input(placeholder=request.ack_word, id="confirm-ack")
        self._focus = capture_focus()

    def compose(self) -> ComposeResult:
        request = self.request
        with Vertical(id="confirm-dialog"):
            yield Label(f"[b]{request.title}[/b]", classes="title")
            if request.summary:
                yield Static("\n".join(request.summary), classes="muted")
            if request.warnings:
                yield Static("\n".join(f"!! {line}" for line in request.warnings), classes="danger")
            yield Label("[b]Dry run (exact commands to be executed):[/b]")
            with VerticalScroll(id="confirm-commands"):
                yield Static(self._commands_text())
            yield Static(f"Type [b]{request.ack_word}[/b] and press Enter to unlock execution:")
            yield self._ack
            yield self._error
            with Horizontal(id="confirm-actions"):
                yield Button("Execute", id="confirm-execute")
                yield Button("Cancel [Esc]", id="confirm-cancel")

    def on_mount(self) -> None:
        self._focus = capture_focus()
        try:
            self.query_one("#confirm-commands").can_focus = False
        except Exception:  # pragma: no cover
            pass
        self._ack.focus()
        payload = {
            "ack_required": self.request.ack_word,
            "commands": self.request.commands,
            "audit_token": self.request.audit_token,
        }
        if self.request.event_prefix == "raid_confirm":
            self.state.evidence.emit("raid_confirm_shown", action=self.request.action, **payload)
        else:
            self.state.evidence.emit(
                "confirm_modal_shown",
                disk_count=len(self.request.commands),
                **payload,
            )
        self._snapshot()

    def on_unmount(self) -> None:
        self._focus.restore()

    def _commands_text(self) -> str:
        return "\n".join(
            f"{index:>3}. {command}" for index, command in enumerate(self.request.commands, start=1)
        ) or "(no commands)"

    def _snapshot(self) -> None:
        if not self.state.cfg.evidence.snapshot_on_screen_change:
            return
        name = self.request.snapshot_name

        async def _capture() -> None:
            try:
                await self.app.take_snapshot(name, epoch=self.request.epoch)  # type: ignore[attr-defined]
            except Exception:  # pragma: no cover
                pass

        self.run_worker(_capture, name=f"snapshot-{name}")

    def _try_approve(self) -> None:
        typed = self._ack.value.strip()
        if typed.lower() != self.request.ack_word.lower():
            self._error.update(f'type "{self.request.ack_word}" to confirm - nothing was executed')
            self._emit_rejected(typed)
            self._ack.value = ""
            self._ack.focus()
            self.app.bell()
            return
        self._emit_accepted()
        self.dismiss(True)

    def _emit_rejected(self, typed: str) -> None:
        if self.request.event_prefix == "raid_confirm":
            self.state.evidence.emit("raid_confirm_rejected", action=self.request.action, typed=typed)
        else:
            self.state.evidence.emit("confirm_ack_rejected", typed=typed)

    def _emit_accepted(self) -> None:
        if self.request.event_prefix == "raid_confirm":
            self.state.evidence.emit(
                "raid_confirm_accepted", action=self.request.action, ack=self.request.ack_word
            )
        else:
            self.state.evidence.emit("confirm_ack_accepted", ack=self.request.ack_word)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "confirm-ack":
            self._try_approve()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "confirm-execute":
            self._try_approve()
        else:
            self.action_cancel()

    def action_cancel(self) -> None:
        if self.request.event_prefix == "raid_confirm":
            self.state.evidence.emit(
                "raid_confirm_cancelled",
                action=self.request.action,
                commands=len(self.request.commands),
            )
        else:
            self.state.evidence.emit(
                "confirm_cancelled",
                disk_count=len(self.request.commands),
            )
        self.dismiss(False)


__all__ = ["ConfirmModal"]
