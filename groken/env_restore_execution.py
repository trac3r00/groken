from __future__ import annotations

from dataclasses import replace
from datetime import UTC

from .env_collectors import NativePlaneUnavailable
from .env_restore_drift import build_restore_report
from .env_restore_inventory import InventoryIndex, inventory_index
from .env_restore_plan import RestoreOperation, RestorePlan
from .env_restore_report import RestoreContext, RestoreReport
from .env_restore_run import (
    RestoreRunRequest,
    RestoreRunResult,
    restore_idempotency_key,
)
from .env_restore_store import (
    JournalEntry,
    JournalState,
    JournalStore,
    JournalUnsafeError,
    RestoreJournal,
)
from .env_restore_validation import Provider


def _journal_entries(plan: RestorePlan) -> tuple[JournalEntry, ...]:
    return tuple(
        JournalEntry(
            row.key,
            row.item,
            row.argv,
            JournalState.PENDING,
            0,
            None,
            None,
            None,
            None,
            None,
            False,
            None,
        )
        for row in plan.operations
    )


def _present(operation: RestoreOperation, current: InventoryIndex) -> bool:
    if operation.provider is Provider.ROUTINE:
        return True
    return bool(operation.verifies) and all(
        current.find(item.provider, item.scope, item.item) is not None
        for item in operation.verifies
    )


def _save_state(
    store: JournalStore,
    journal: RestoreJournal,
    entry: JournalEntry,
) -> RestoreJournal:
    return store.save(
        tuple(entry if row.key == entry.key else row for row in journal.operations)
    )


def _manual_result(result: RestoreRunResult) -> bool:
    detail = (result.stdout + result.stderr).decode(errors="replace").casefold()
    return result.exit_code != 0 and (
        "sign in" in detail or "signed in" in detail or "apple id" in detail
    )


def _running_entry(
    context: RestoreContext,
    entry: JournalEntry,
    operation: RestoreOperation,
) -> JournalEntry:
    if entry.state is JournalState.RUNNING:
        if entry.idempotency_key is None or entry.attempts < 1:
            raise JournalUnsafeError(
                "running restore operation has no persisted attempt key"
            )
        return entry
    attempt = entry.attempts + 1
    started = context.now().astimezone(UTC).isoformat().replace("+00:00", "Z")
    return replace(
        entry,
        state=JournalState.RUNNING,
        attempts=attempt,
        idempotency_key=restore_idempotency_key(
            context.store.manifest_id,
            operation.key,
            attempt,
        ),
        started_at=started,
        ended_at=None,
        exit_code=None,
        signal=None,
        truncated=False,
        error=None,
    )


def execute_restore(plan: RestorePlan, context: RestoreContext) -> RestoreReport:
    with context.store.lock():
        journal = context.store.ensure(_journal_entries(plan))
        initial = inventory_index(context.recapture())
        for operation in plan.operations:
            entry = journal.find(operation.key)
            if entry.state is JournalState.MANUAL and not context.options.retry_manual:
                continue
            if entry.state is JournalState.SUCCEEDED and _present(operation, initial):
                continue
            if (
                entry.state is JournalState.PENDING
                and operation.provider is not Provider.ROUTINE
                and _present(operation, initial)
            ):
                journal = _save_state(
                    context.store,
                    journal,
                    replace(entry, state=JournalState.SUCCEEDED),
                )
                continue
            if operation.manual_reason is not None and not operation.argv:
                journal = _save_state(
                    context.store,
                    journal,
                    replace(
                        entry, state=JournalState.MANUAL, error=operation.manual_reason
                    ),
                )
                continue
            running = _running_entry(context, entry, operation)
            if running is not entry:
                journal = _save_state(context.store, journal, running)
            if running.idempotency_key is None:
                raise JournalUnsafeError(
                    "restore operation has no persisted idempotency key"
                )
            request = RestoreRunRequest(
                context.store.manifest_id,
                operation.key,
                running.attempts,
                running.idempotency_key,
                operation.argv,
                b"",
                30_000,
            )
            try:
                result = context.runner.run_restore(request)
            except (NativePlaneUnavailable, OSError) as exc:
                ended = context.now().astimezone(UTC).isoformat().replace("+00:00", "Z")
                journal = _save_state(
                    context.store,
                    journal,
                    replace(
                        running,
                        state=JournalState.FAILED,
                        ended_at=ended,
                        error=str(exc),
                    ),
                )
                break
            ended = context.now().astimezone(UTC).isoformat().replace("+00:00", "Z")
            raw_outcome = (
                (result.stdout + result.stderr).decode(errors="replace").strip()
            )
            if result.argv != operation.argv:
                state, error = JournalState.FAILED, "runner returned a different argv"
            elif _manual_result(result):
                state, error = JournalState.MANUAL, raw_outcome
            elif (
                result.exit_code == 0 and not result.timed_out and result.signal is None
            ):
                state, error = JournalState.SUCCEEDED, None
            else:
                state = JournalState.FAILED
                error = raw_outcome or (
                    f"restore command terminated by signal {result.signal}"
                    if result.signal is not None
                    else "restore command timed out"
                    if result.timed_out
                    else "restore command failed"
                )
            journal = _save_state(
                context.store,
                journal,
                replace(
                    running,
                    state=state,
                    ended_at=ended,
                    exit_code=result.exit_code,
                    signal=result.signal,
                    truncated=result.truncated,
                    error=error,
                ),
            )
        final = inventory_index(context.recapture())
        for operation in plan.operations:
            entry = journal.find(operation.key)
            if entry.state is JournalState.SUCCEEDED and not _present(operation, final):
                journal = _save_state(
                    context.store,
                    journal,
                    replace(
                        entry,
                        state=JournalState.FAILED,
                        error="item verification failed",
                    ),
                )
        return build_restore_report(plan, final, journal)
