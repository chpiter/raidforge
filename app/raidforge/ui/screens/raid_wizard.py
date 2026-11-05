"""Four-step RAID creation wizard.

Step 4 is the shared ConfirmModal: step 3 builds the layout and command list,
then the modal provides the acknowledgement gate.
"""

from __future__ import annotations

from typing import List, Optional

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, ContentSwitcher, DataTable, Input, Static

from ...agent.queue import submit_local
from ...core.models import Raid0Mode, RaidAction, RaidCommandPlan, RaidLayout, RaidLevel
from ..focus import FocusLabelMixin, capture_focus
from ..models import ConfirmRequest, HotkeyHint, TaskKind
from ..state import AppState
from ..tasks import make_task, raid_job
from ..widgets.breadcrumb import Breadcrumb
from ..widgets.disk_table import MultiSelectTable
from ..widgets.hotkey_bar import HotkeyBar

STEPS = [("level", "1 Level"), ("disks", "2 Disks"), ("preview", "3 Preview"), ("confirm", "4 Confirm")]


class RaidWizardScreen(FocusLabelMixin, Screen):
    BINDINGS = [
        Binding("escape", "back", "Back", show=False, priority=True),
        Binding("enter", "next_step", "Next", show=False, priority=True),
        Binding("0", "level('0')", "RAID0", show=False),
        Binding("1", "level('1')", "RAID1", show=False),
        Binding("5", "level('5')", "RAID5", show=False),
        Binding("t", "level('10')", "RAID10", show=False),
    ]

    def __init__(self, state: AppState, controller_index: int, preselected: Optional[List[str]] = None) -> None:
        super().__init__()
        self.state = state
        self.controller_index = controller_index
        self.level = RaidLevel.RAID1
        self.mode = Raid0Mode.SINGLE
        self.selected = set(preselected or [])
        self.raid_layout: Optional[RaidLayout] = None
        self.plans: List[RaidCommandPlan] = []
        self.warnings: List[str] = []
        self.step = 0

    # -- layout -------------------------------------------------------------
    def compose(self) -> ComposeResult:
        with Vertical(classes="chrome-screen"):
            yield Breadcrumb("", id="wizard-breadcrumb")
            yield Static("", id="wizard-steps")
            with ContentSwitcher(initial="wizard-level", id="wizard-pages"):
                with Vertical(id="wizard-level", classes="wizard-page"):
                    yield Static("Choose a RAID level and volume name.", classes="muted")
                    with Horizontal(classes="level-grid"):
                        yield Button("RAID0", id="lvl-0", classes="level-button")
                        yield Button("RAID1", id="lvl-1", classes="level-button")
                        yield Button("RAID5", id="lvl-5", classes="level-button")
                        yield Button("RAID10", id="lvl-10", classes="level-button")
                    with Horizontal(classes="level-grid"):
                        yield Button("single VD", id="mode-single", classes="level-button")
                        yield Button("one VD per disk", id="mode-perdisk", classes="level-button")
                    yield Input(placeholder="volume name (optional)", id="wizard-name")
                    with Horizontal(classes="form-actions"):
                        yield Button("Next [Enter]", id="wizard-next")
                with Vertical(id="wizard-disks", classes="wizard-page"):
                    yield Static("Select free disks. Disks already in a VD are dim and cannot be ticked.", classes="muted")
                    yield MultiSelectTable(
                        id="wizard-disks-table",
                        row_allowed=self._row_allowed,
                        vd_for=self._vd_label,
                        on_change=self._on_selection,
                    )
                    with Horizontal(classes="form-actions"):
                        yield Button("Back [Esc]", id="wizard-back")
                        yield Button("Preview [Enter]", id="wizard-preview-next")
                with Vertical(id="wizard-preview", classes="wizard-page"):
                    yield Static("", id="wizard-preview-body")
                    yield VerticalScroll(id="wizard-commands")
                    with Horizontal(classes="form-actions"):
                        yield Button("Back [Esc]", id="wizard-preview-back")
                        yield Button("Confirm [Enter]", id="wizard-confirm")
                with Vertical(id="wizard-confirm", classes="wizard-page"):
                    yield Static("Confirming in the modal ...", classes="muted")
            yield HotkeyBar("", id="wizard-hotkeys")

    def on_mount(self) -> None:
        self._focus = capture_focus()
        self.state.push_breadcrumb(f"create on c{self.controller_index}")
        self._refresh_level_buttons()
        self._load_disks()
        self._set_step(0)

    def on_unmount(self) -> None:
        self._focus.restore()
        self.state.pop_breadcrumb()

    # -- data ---------------------------------------------------------------
    def _load_disks(self) -> None:
        disks = self.state.controller_disks(self.controller_index)
        try:
            self.query_one(MultiSelectTable).set_layout_mode(self.state.layout_mode)
            self.query_one(MultiSelectTable).load_disks(disks, self.selected, self.state.physical_cursor)
        except Exception:  # pragma: no cover - initial compose
            pass

    def _row_allowed(self, disk):
        reason = self.state.blocked_reason(disk)
        return (not reason, reason)

    def _vd_label(self, disk) -> str:
        membership = self.state.disk_membership().get(disk.disk_id)
        if membership:
            return membership
        return "blocked" if self.state.blocked_reason(disk) else "free"

    def _on_selection(self, message: str) -> None:
        try:
            self.selected = set(self.query_one(MultiSelectTable).selected_ids)
        except Exception:  # pragma: no cover
            pass
        self._chrome()

    # -- steps --------------------------------------------------------------
    def _set_step(self, index: int) -> None:
        self.step = max(0, min(index, len(STEPS) - 1))
        name = STEPS[self.step][0]
        try:
            self.query_one(ContentSwitcher).current = f"wizard-{name}"
        except Exception:  # pragma: no cover
            pass
        self._render_steps()
        self._chrome()
        if self.step == 0:
            try:
                self.query_one("#lvl-1", Button).focus()
            except Exception:  # pragma: no cover
                pass
        elif self.step == 1:
            self._load_disks()
            try:
                self.query_one(MultiSelectTable).focus()
            except Exception:  # pragma: no cover
                pass
        elif self.step == 2:
            def _focus_confirm() -> None:
                try:
                    self.query_one("#wizard-confirm", Button).focus()
                except Exception:  # pragma: no cover
                    pass

            self.call_after_refresh(_focus_confirm)
        self._snapshot(f"17{'abcd'[self.step]}-wizard-{name}")

    def _render_steps(self) -> None:
        text = Text()
        for index, (_key, label) in enumerate(STEPS):
            if index:
                text.append("  ")
            if index < self.step:
                text.append(f"✓ {label}", style="bold")
            elif index == self.step:
                text.append(f"▶ {label}", style="bold reverse")
            else:
                text.append(f"○ {label}")
        try:
            self.query_one("#wizard-steps", Static).update(text)
        except Exception:  # pragma: no cover
            pass

    def _chrome(self) -> None:
        try:
            self.query_one(Breadcrumb).update_items(
                self.state.breadcrumb_items() + []
            )
            hints = self._hints()
            self.query_one(HotkeyBar).update_hints(hints, self.app.theme)
        except Exception:  # pragma: no cover
            pass

    def _hints(self) -> List[HotkeyHint]:
        if self.step == 0:
            return [
                HotkeyHint("0/1/5/T", "level"),
                HotkeyHint("Enter", "next"),
                HotkeyHint("Esc", "back"),
            ]
        if self.step == 1:
            return [
                HotkeyHint("Space", "toggle"),
                HotkeyHint("Ctrl+A", "all"),
                HotkeyHint("Ctrl+I", "invert"),
                HotkeyHint("Enter", "preview"),
                HotkeyHint("Esc", "back"),
            ]
        if self.step == 2:
            return [
                HotkeyHint("Enter", "confirm", danger=True, enabled=bool(self.plans)),
                HotkeyHint("Esc", "back"),
            ]
        return [HotkeyHint("Esc", "back")]

    # -- level --------------------------------------------------------------
    def action_level(self, number: str) -> None:
        if self.step != 0:
            return
        self.level = {
            "0": RaidLevel.RAID0,
            "1": RaidLevel.RAID1,
            "5": RaidLevel.RAID5,
            "10": RaidLevel.RAID10,
        }.get(number, self.level)
        self._refresh_level_buttons()

    def _refresh_level_buttons(self) -> None:
        mapping = {
            "lvl-0": RaidLevel.RAID0,
            "lvl-1": RaidLevel.RAID1,
            "lvl-5": RaidLevel.RAID5,
            "lvl-10": RaidLevel.RAID10,
        }
        for button_id, level in mapping.items():
            try:
                button = self.query_one(f"#{button_id}", Button)
                button.label = f"[X] {level.value}" if level is self.level else level.value
            except Exception:  # pragma: no cover
                pass
        for button_id, mode in (("mode-single", Raid0Mode.SINGLE), ("mode-perdisk", Raid0Mode.PER_DISK)):
            try:
                button = self.query_one(f"#{button_id}", Button)
                button.label = f"[X] {mode.value}" if mode is self.mode else mode.value
            except Exception:  # pragma: no cover
                pass

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if button_id.startswith("lvl-"):
            self.action_level(button_id.split("-", 1)[1])
            return
        if button_id == "mode-single":
            self.mode = Raid0Mode.SINGLE
            self._refresh_level_buttons()
            return
        if button_id == "mode-perdisk":
            self.mode = Raid0Mode.PER_DISK
            self._refresh_level_buttons()
            return
        if button_id == "wizard-next":
            self._set_step(1)
            return
        if button_id == "wizard-preview-next":
            self._build_preview()
            self._set_step(2)
            return
        if button_id == "wizard-preview-back":
            self._set_step(1)
            return
        if button_id == "wizard-back":
            self._set_step(max(0, self.step - 1))
            return
        if button_id == "wizard-confirm":
            self._confirm()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "wizard-name":
            self._set_step(1)

    # -- preview ------------------------------------------------------------
    def _build_preview(self) -> None:
        services = self.state.services
        name = ""
        try:
            name = self.query_one("#wizard-name", Input).value.strip()
        except Exception:  # pragma: no cover
            pass
        try:
            layout, plans, warnings = services.plan_create(  # type: ignore[union-attr]
                self.controller_index, self.level, sorted(self.selected), self.mode, name
            )
        except Exception as exc:
            layout, plans, warnings = None, [], [str(exc)]
        self.raid_layout = layout
        self.plans = list(plans)
        self.warnings = list(warnings)
        if layout is not None:
            self.warnings = list(layout.warnings) + list(warnings)
        text = Text()
        text.append("Level    ", style="bold")
        text.append(f"{self.level.value}   ")
        text.append("Mode ", style="bold")
        text.append(f"{self.mode.value}   ")
        text.append("Name ", style="bold")
        text.append(f"{name or '-'}\n")
        if layout is not None:
            text.append("Capacity ", style="bold")
            text.append(f"{layout.capacity_human}\n")
            text.append("Stripe   ", style="bold")
            text.append(f"{layout.stripe_kb}K\n" if layout.stripe_kb else "controller default\n")
            text.append("Init     ", style="bold")
            text.append(f"{layout.init}\n")
            text.append("Spans    ", style="bold")
            text.append(f"{len(layout.spans)}\n")
            for index, span in enumerate(layout.spans, start=1):
                text.append(f"  {index}) " + " + ".join(span) + "\n")
        if self.warnings:
            text.append("Warnings\n", style="bold")
            for warning in self.warnings[:8]:
                text.append(f"  ⚠ {warning}\n")
        else:
            text.append("Warnings  ✓ none\n", style="bold")
        try:
            self.query_one("#wizard-preview-body", Static).update(text)
            log = self.query_one("#wizard-commands", VerticalScroll)
            for child in list(log.children):
                child.remove()
            commands = "\n".join(
                f"{index:>3}. {plan.dry_run_text}" for index, plan in enumerate(self.plans, start=1)
            ) or "(no commands - fix the warnings above)"
            log.mount(Static(commands, classes="command-list"))
        except Exception:  # pragma: no cover
            pass

    # -- confirm ------------------------------------------------------------
    def _confirm(self) -> None:
        if not self.plans:
            self._chrome()
            return
        epoch = self.state.raid_epoch + 1
        self.state.raid_epoch = epoch
        spans = []
        if self.raid_layout is not None:
            spans = [
                f"  {index}) " + " + ".join(span)
                for index, span in enumerate(self.raid_layout.spans, start=1)
            ]
        stripe = (
            f"{self.raid_layout.stripe_kb}K"
            if self.raid_layout and self.raid_layout.stripe_kb
            else "controller default"
        )
        init = self.raid_layout.init if self.raid_layout else "fast"
        request = ConfirmRequest(
            title=f"Create {self.level.value} on c{self.controller_index}",
            action="create",
            ack_word="yes",
            ack_label="yes",
            audit_token=self.state.audit_token(),
            commands=[plan.dry_run_text for plan in self.plans],
            summary=[
                f"Level {self.level.value}   capacity {self.raid_layout.capacity_human if self.raid_layout else '-'}",
                f"Stripe {stripe}   init {init}",
                *spans,
            ],
            warnings=self.warnings or ["IRREVERSIBLE: the controller may start initialising immediately."],
            danger=True,
            epoch=epoch,
            snapshot_name="04-raid-confirm",
            event_prefix="raid_confirm",
        )

        def after(approved: bool) -> None:
            if not approved:
                self._set_step(2)
                return
            task = make_task(
                TaskKind.RAID_CREATE,
                f"Create {self.level.value} on c{self.controller_index}",
                controller_index=self.controller_index,
                disk_ids=sorted(self.selected),
                epoch=epoch,
            )
            submit_local(
                self.state,
                task,
                raid_job(
                    self.state.services,
                    self.plans,
                    RaidAction.CREATE,
                    self.controller_index,
                    epoch,
                ),
            )
            self.state.last_message = f"create queued as {task.id}"
            self.dismiss(True)

        from .confirm import ConfirmModal

        self._set_step(3)
        self.app.push_screen(ConfirmModal(self.state, request), after)

    # -- navigation ---------------------------------------------------------
    def action_next_step(self) -> None:
        if self.step == 0:
            self._set_step(1)
        elif self.step == 1:
            self._build_preview()
            self._set_step(2)
        elif self.step == 2:
            self._confirm()

    def action_back(self) -> None:
        if self.step > 0:
            self._set_step(self.step - 1)
            return
        self.dismiss(False)

    def _snapshot(self, name: str) -> None:
        if not self.state.cfg.evidence.snapshot_on_screen_change:
            return

        async def _capture() -> None:
            try:
                await self.app.take_snapshot(name)  # type: ignore[attr-defined]
            except Exception:  # pragma: no cover
                pass

        self.run_worker(_capture, name=f"snapshot-{name}")


__all__ = ["RaidWizardScreen"]
