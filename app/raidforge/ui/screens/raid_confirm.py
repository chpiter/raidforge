"""Confirmation gate for every destructive RAID write."""

from __future__ import annotations

from typing import List, Optional

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Static

from ...core.models import RaidCommandPlan
from ..state import AppContext


class RaidConfirmModal(ModalScreen[bool]):
    """Show the exact dry-run commands and require a deliberate acknowledgement.

    Create/delete use ``yes``.  Clearing the whole controller configuration uses
    the controller id (for example ``c0``) so the operator cannot clear a
    controller with muscle memory from the erase flow.
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    CSS = """
    RaidConfirmModal {
        align: center middle;
    }
    #raid-dialog {
        width: 92%;
        height: 92%;
        border: thick $foreground;
        background: $background;
        padding: 0 1;
        overflow-y: auto;
    }
    #raid-commands {
        min-height: 5;
        height: 1fr;
        border: round $foreground;
        padding: 0 1;
        overflow-y: auto;
    }
    #raid-actions {
        height: 3;
    }
    #raid-actions Button {
        margin: 0 2 0 0;
        min-width: 16;
    }
    """

    def __init__(
        self,
        ctx: AppContext,
        plans: List[RaidCommandPlan],
        title: str,
        ack_word: str,
        extra_lines: Optional[List[str]] = None,
        epoch: Optional[int] = None,
    ) -> None:
        super().__init__()
        self.ctx = ctx
        self.plans = plans
        self.title_text = title
        self.ack_word = ack_word
        self.extra_lines = list(extra_lines or [])
        self.epoch = ctx.raid_epoch if epoch is None else epoch
        self._error = Static("", classes="danger")
        self._ack_input = Input(placeholder=ack_word, id="raid-ack", password=False)

    def compose(self) -> ComposeResult:
        with Vertical(id="raid-dialog"):
            yield Label(f"[b]{self.title_text}[/b]", classes="title")
            if self.extra_lines:
                yield Static("\n".join(self.extra_lines), classes="muted")
            yield Static(
                "[b]!! IRREVERSIBLE[/b]: these commands are submitted to the controller. "
                "Member data becomes inaccessible immediately.",
                classes="muted",
            )
            yield Label("[b]Dry run (exact commands to be executed):[/b]")
            with VerticalScroll(id="raid-commands"):
                yield Static(self._render_commands())
            yield Static(f"Type [b]{self.ack_word}[/b] and press Enter to unlock execution:")
            yield self._ack_input
            yield self._error
            with Horizontal(id="raid-actions"):
                yield Button("Execute", id="raid-execute")
                yield Button("Cancel [Esc]", id="raid-cancel")

    def on_mount(self) -> None:
        action = self.plans[0].action.value if self.plans else "none"
        self.ctx.evidence.emit(
            "raid_confirm_shown",
            action=action,
            ack_required=self.ack_word,
            commands=[plan.to_json_obj() for plan in self.plans],
        )
        self.query_one("#raid-ack", Input).focus()
        self._schedule_snapshot("04-raid-confirm")

    def _schedule_snapshot(self, name: str) -> None:
        if not self.ctx.cfg.evidence.snapshot_on_screen_change:
            return

        async def _capture() -> None:
            try:
                await self.app.take_snapshot(name, epoch=self.epoch)  # type: ignore[attr-defined]
            except Exception:  # pragma: no cover - screenshots are best effort
                pass

        self.run_worker(_capture, name=f"snapshot-{name}")

    def _render_commands(self) -> str:
        return "\n".join(
            f"{index:>3}. {plan.dry_run_text}" for index, plan in enumerate(self.plans, start=1)
        ) or "(no commands)"

    def _try_approve(self) -> None:
        action = self.plans[0].action.value if self.plans else "none"
        typed = self._ack_input.value.strip()
        if typed.lower() != self.ack_word.lower():
            self._error.update(f'type "{self.ack_word}" to confirm - nothing was executed')
            self.ctx.evidence.emit("raid_confirm_rejected", action=action, typed=typed)
            self._ack_input.value = ""
            self._ack_input.focus()
            self.app.bell()
            return
        self.ctx.evidence.emit("raid_confirm_accepted", action=action, ack=self.ack_word)
        self.dismiss(True)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "raid-ack":
            self._try_approve()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "raid-execute":
            self._try_approve()
        else:
            self.action_cancel()

    def action_cancel(self) -> None:
        action = self.plans[0].action.value if self.plans else "none"
        self.ctx.evidence.emit("raid_confirm_cancelled", action=action, commands=len(self.plans))
        self.dismiss(False)


__all__ = ["RaidConfirmModal"]
