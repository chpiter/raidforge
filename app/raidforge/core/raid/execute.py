"""Serial RAID command execution.

The executor mirrors :mod:`raidforge.core.erase`: commands are submitted one at
a time, ``rc == 0`` is recorded as *accepted by the controller*, and the tool
never claims the controller finished the asynchronous work.  A failed step does
not stop the remaining steps (clear-config is multi-step and one-VD-per-disk
RAID0 is multi-command); every outcome is recorded.
"""

from __future__ import annotations

import logging
from typing import Callable, List, Optional

from ..config import Config
from ..evidence import EvidenceSession
from ..models import RaidAction, RaidActionResult, RaidCommandPlan, RaidStatus
from ..utils import RC_TIMEOUT, run_cmd, tail_lines

log = logging.getLogger(__name__)

ProgressCallback = Callable[[int, int, RaidActionResult], None]
StopCallback = Callable[[], bool]


def execute_raid_plans(
    plans: List[RaidCommandPlan],
    evidence: EvidenceSession,
    cfg: Config,
    on_progress: Optional[ProgressCallback] = None,
    should_stop: Optional[StopCallback] = None,
) -> List[RaidActionResult]:
    """Run every plan serially and append audit events.

    Returns:
        One result per submitted plan.  Plans skipped because the operator
        stopped the run are absent (the run summary records them as
        ``not_attempted``).
    """

    action = plans[0].action if plans else RaidAction.CREATE
    total = len(plans)
    results: List[RaidActionResult] = []
    cancelled = False

    evidence.emit(
        "raid_run_started",
        action=action.value,
        total=total,
        dry_run=cfg.dry_run,
        plans=[plan.to_json_obj() for plan in plans],
    )

    timeout = getattr(cfg.raid, "command_timeout", cfg.command_timeout)
    for index, plan in enumerate(plans, start=1):
        if should_stop is not None and should_stop():
            cancelled = True
            remaining = [item.label for item in plans[index - 1 :]]
            evidence.emit("raid_run_cancelled", action=action.value, at_index=index - 1, remaining=remaining)
            log.warning("RAID run cancelled - %d command(s) not submitted", len(remaining))
            break

        if cfg.dry_run:
            result = RaidActionResult(
                action=plan.action,
                controller_index=plan.controller_index,
                status=RaidStatus.SKIPPED,
                return_code=None,
                duration_sec=0.0,
                argv=list(plan.argv),
                label=plan.label,
                stderr="dry-run enabled: nothing was submitted to the controller",
            )
            evidence.emit("raid_submit", **result.to_json_obj())
            results.append(result)
            if on_progress:
                on_progress(index, total, result)
            continue

        evidence.emit(
            "raid_submit_started",
            action=plan.action.value,
            label=plan.label,
            argv=list(plan.argv),
            command=plan.dry_run_text,
        )
        outcome = run_cmd(plan.argv, timeout=timeout)
        if outcome.return_code == 0:
            status = RaidStatus.ACCEPTED
        elif outcome.timed_out or outcome.return_code == RC_TIMEOUT:
            status = RaidStatus.TIMEOUT
        else:
            status = RaidStatus.FAILED

        result = RaidActionResult(
            action=plan.action,
            controller_index=plan.controller_index,
            status=status,
            return_code=outcome.return_code,
            duration_sec=outcome.duration,
            argv=list(plan.argv),
            label=plan.label,
            stdout=tail_lines(outcome.stdout, limit=60),
            stderr=tail_lines(outcome.stderr, limit=60),
        )
        evidence.emit("raid_submit", **result.to_json_obj())
        log.info("RAID submit %s -> %s", plan.label, status.value)
        results.append(result)
        if on_progress:
            on_progress(index, total, result)

    summary = {
        "total": total,
        "action": action.value,
        "cancelled": cancelled,
        "accepted": sum(1 for result in results if result.status is RaidStatus.ACCEPTED),
        "failed": sum(1 for result in results if result.status is RaidStatus.FAILED),
        "timeout": sum(1 for result in results if result.status is RaidStatus.TIMEOUT),
        "skipped": sum(1 for result in results if result.status is RaidStatus.SKIPPED),
    }
    evidence.emit("raid_run_finished", **summary)
    return results


__all__ = ["execute_raid_plans"]
