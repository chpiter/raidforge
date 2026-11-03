"""Confirmation modal (constraint 8).

Nothing can reach :class:`~raidforge.ui.screens.progress.ProgressScreen` without
passing through here.  The screen shows:

* the exact, shell-quoted dry-run command list;
* the destination disks with their media type and capacity;
* a short acknowledgement field: the operator types ``yes`` to unlock.

The acknowledgement used to be ``CONFIRM-{server_sn}``.  That proved far too
much typing on a console keyboard, and a long string typed blind is its own
hazard - a short, deliberate ``yes`` keeps the gate meaningful without inviting
typos.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Static

from ...core.models import CommandPlan, DiskInfo
from ..state import AppContext

#: The operator types this, case-insensitively, to unlock execution.
ACK_WORD = "yes"


class ConfirmModal(ModalScreen[bool]):
    """Last gate before anything is written to a controller."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    CSS = """
    ConfirmModal {
        align: center middle;
    }
    #dialog {
        width: 92%;
        height: 92%;
        border: thick $foreground;
        background: $background;
        padding: 0 1;
        /* An 80x25 console leaves ~22 usable rows; let the dialog scroll rather
           than silently clipping the command list or the buttons. */
        overflow-y: auto;
    }
    #commands {
        /* Never collapse to nothing: the dry-run commands are the whole point. */
        min-height: 5;
        height: 1fr;
        border: round $foreground;
        padding: 0 1;
        overflow-y: auto;
    }
    #actions {
        /* Fixed height: with `auto` the 3-row buttons get clipped to one line on
           an 80x25 console and lose their borders. */
        height: 3;
    }
    #actions Button {
        margin: 0 2 0 0;
        min-width: 16;
    }
    """

    def __init__(
        self,
        ctx: AppContext,
        plans: List[CommandPlan],
        disks: Optional[Dict[str, DiskInfo]] = None,
    ) -> None:
        super().__init__()
        self.ctx = ctx
        self.plans = plans
        self.disks = dict(disks or {})
        self._error = Static("", classes="danger")
        self._ack_input = Input(placeholder=ACK_WORD, id="ack", password=False)

    # -- layout -------------------------------------------------------------
    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(f"[b]Confirm erasure of {len(self.plans)} disk(s)[/b]", classes="title")
            yield Static(self._disk_summary(), classes="muted")
            yield Static(
                "[b]!! IRREVERSIBLE[/b]: the commands below are only *submitted* to the "
                "controller - the erase itself continues asynchronously.",
                classes="muted",
            )
            yield Label("[b]Dry run (exact commands to be executed):[/b]")
            with VerticalScroll(id="commands"):
                yield Static(self._render_commands())
            yield Static(f"Type [b]{ACK_WORD}[/b] and press Enter to unlock execution:")
            yield self._ack_input
            yield self._error
            with Horizontal(id="actions"):
                # No `variant=`: the palette has exactly two colours.  The label
                # and the focus highlight carry the meaning.
                yield Button("Execute (erase)", id="execute")
                yield Button("Cancel [Esc]", id="cancel")

    def on_mount(self) -> None:
        """Focus the acknowledgement field and record that the modal appeared."""

        self.ctx.evidence.emit(
            "confirm_modal_shown",
            disk_count=len(self.plans),
            ack_required=ACK_WORD,
            commands=[plan.to_json_obj() for plan in self.plans],
        )
        self.query_one("#ack", Input).focus()
        self._schedule_snapshot("04-confirm")

    def _schedule_snapshot(self, name: str) -> None:
        """Capture the rendered modal - it is the key audit artefact."""

        if not self.ctx.cfg.evidence.snapshot_on_screen_change:
            return

        async def _capture() -> None:
            try:
                await self.app.take_snapshot(name, epoch=self.ctx.run_epoch)  # type: ignore[attr-defined]
            except Exception:  # pragma: no cover - screenshots are best effort
                pass

        self.run_worker(_capture, name=f"snapshot-{name}")

    # -- helpers ------------------------------------------------------------
    def _disk_summary(self) -> str:
        lines: List[str] = []
        for plan in self.plans:
            disk = self.disks.get(plan.disk_id) or self.ctx.disk_by_id(plan.disk_id)
            size = disk.size_human if disk else "-"
            lines.append(f"  {plan.disk_id:<20} {plan.medium.value:<8} {size:<12} {plan.label}")
        return "\n".join(lines) or "  (none)"

    def _render_commands(self) -> str:
        numbered = [f"{index:>3}. {plan.dry_run_text}" for index, plan in enumerate(self.plans, start=1)]
        return "\n".join(numbered) or "(no commands)"

    def _try_approve(self) -> None:
        """Validate the acknowledgement word and dismiss accordingly."""

        typed = self._ack_input.value.strip()
        if typed.lower() != ACK_WORD:
            self._error.update(f'type "{ACK_WORD}" to confirm - nothing was executed')
            self.ctx.evidence.emit("confirm_ack_rejected", typed=typed)
            # Clear the field: a rejected attempt must not be silently extended
            # into a valid one by whatever the operator types next.
            self._ack_input.value = ""
            self._ack_input.focus()
            self.app.bell()
            return
        self.ctx.evidence.emit("confirm_ack_accepted", ack=ACK_WORD)
        self.dismiss(True)

    # -- events -------------------------------------------------------------
    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Enter inside the acknowledgement field attempts approval."""

        if event.input.id == "ack":
            self._try_approve()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle the two action buttons."""

        if event.button.id == "execute":
            self._try_approve()
        else:
            self.action_cancel()

    def action_cancel(self) -> None:
        """Dismiss without approval."""

        self.ctx.evidence.emit("confirm_cancelled", disk_count=len(self.plans))
        self.dismiss(False)
