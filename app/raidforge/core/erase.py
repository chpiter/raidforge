"""Command execution layer (constraint 12).

Erase commands are executed **strictly serially**.  Each command is given
``tools.command_timeout`` seconds (default 60) and is considered accepted as
soon as the vendor tool returns zero.  We deliberately do *not* poll the
controller afterwards: every vendor implements erase as an asynchronous task,
so the only honest statement this tool can make is "the command was submitted".
Results are therefore recorded as :attr:`~raidforge.core.models.SubmitStatus.ASYNC_SUBMITTED`.
"""

from __future__ import annotations

import logging
from typing import Callable, List, Optional

from ..config import Config
from .evidence import EvidenceSession
from .models import CommandPlan, DiskInfo, SubmitResult, SubmitStatus
from .utils import RC_TIMEOUT, run_cmd, tail_lines

log = logging.getLogger(__name__)

ProgressCallback = Callable[[int, int, SubmitResult], None]
StopCallback = Callable[[], bool]


def execute_plans(
    plans: List[CommandPlan],
    evidence: EvidenceSession,
    cfg: Config,
    on_progress: Optional[ProgressCallback] = None,
    should_stop: Optional[StopCallback] = None,
) -> List[SubmitResult]:
    """Run every plan serially and record the outcome.

    Args:
        plans: Commands to run, in operator order.
        evidence: Session used to append audit events.
        cfg: Loaded configuration (timeout, dry-run flag).
        on_progress: Optional callback ``(done, total, result)`` invoked after
            each submission so the UI can repaint its progress screen.
        should_stop: Optional predicate polled before every command.  Once it
            returns True the remaining plans are **not** submitted - the run is
            cut short and the unsubmitted disks are written to the evidence log
            so the audit trail says what did *not* happen.

    Returns:
        One :class:`SubmitResult` per submitted plan, in the same order.
    """

    results: List[SubmitResult] = []
    total = len(plans)
    evidence.emit("erase_run_started", total=total, dry_run=cfg.dry_run,
                  plans=[p.to_json_obj() for p in plans])
    cancelled = False

    for index, plan in enumerate(plans, start=1):
        if should_stop is not None and should_stop():
            cancelled = True
            remaining = [p.disk_id for p in plans[index - 1:]]
            # Audit side of a cancel: the operator must be able to prove which
            # disks were never touched.
            evidence.emit("erase_run_cancelled", at_index=index - 1, remaining=remaining)
            log.warning("erase run cancelled - %d plan(s) not submitted", len(remaining))
            break

        if cfg.dry_run:
            result = SubmitResult(
                disk_id=plan.disk_id,
                kind=plan.kind,
                status=SubmitStatus.SKIPPED,
                return_code=None,
                duration_sec=0.0,
                stderr="dry-run enabled: nothing was submitted to the controller",
                argv=plan.argv,
            )
            evidence.emit("erase_submit", **result.to_json_obj())
            results.append(result)
            if on_progress:
                on_progress(index, total, result)
            continue

        if should_stop is not None and should_stop():
            cancelled = True
            remaining = [p.disk_id for p in plans[index - 1:]]
            evidence.emit("erase_run_cancelled", at_index=index - 1, remaining=remaining)
            log.warning("erase run cancelled just before %s", plan.disk_id)
            break

        evidence.emit(
            "erase_submit_started",
            disk_id=plan.disk_id,
            kind=plan.kind,
            argv=plan.argv,
            command=plan.dry_run_text,
        )
        outcome = run_cmd(plan.argv, timeout=cfg.command_timeout)

        if outcome.return_code == 0:
            status = SubmitStatus.ASYNC_SUBMITTED
        elif outcome.timed_out or outcome.return_code == RC_TIMEOUT:
            status = SubmitStatus.TIMEOUT
        else:
            status = SubmitStatus.FAILED

        result = SubmitResult(
            disk_id=plan.disk_id,
            kind=plan.kind,
            status=status,
            return_code=outcome.return_code,
            duration_sec=outcome.duration,
            stdout=tail_lines(outcome.stdout, limit=60),
            stderr=tail_lines(outcome.stderr, limit=60),
            argv=plan.argv,
        )
        log.info("erase submit %s -> %s", plan.disk_id, status.value)
        evidence.emit("erase_submit", **result.to_json_obj())
        results.append(result)
        if on_progress:
            on_progress(index, total, result)

    summary = {
        "total": total,
        "cancelled": cancelled,
        "submitted": sum(1 for r in results if r.ok),
        "failed": sum(1 for r in results if r.status is SubmitStatus.FAILED),
        "timeout": sum(1 for r in results if r.status is SubmitStatus.TIMEOUT),
        "skipped": sum(1 for r in results if r.status is SubmitStatus.SKIPPED),
    }
    evidence.emit("erase_run_finished", **summary, disks=[r.disk_id for r in results])
    return results


def selection_is_erasable(disks: List[DiskInfo]) -> bool:
    """True when the selection contains at least one selectable disk.

    Constraint 8: any disk whose media type is UNKNOWN must be rejected before
    we even reach plan construction.
    """

    return any(disk.selectable for disk in disks)


def rejected_disks(disks: List[DiskInfo]) -> List[DiskInfo]:
    """Return the disks that are forbidden from taking part in an erase."""

    return [disk for disk in disks if not disk.selectable]
