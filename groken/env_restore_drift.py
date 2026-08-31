from __future__ import annotations

from .env_restore_inventory import InventoryIndex, InventoryItem
from .env_restore_plan import RestoreOperation, RestorePlan
from .env_restore_report import ReportClass, ReportItem, RestoreReport
from .env_restore_store import JournalState, RestoreJournal
from .env_restore_validation import Provider


def _operation_for(
    plan: RestorePlan, expected: InventoryItem
) -> RestoreOperation | None:
    return next(
        (
            operation
            for operation in plan.operations
            if any(
                item.provider is expected.provider
                and item.scope == expected.scope
                and item.item.casefold() == expected.item.casefold()
                for item in operation.verifies
            )
        ),
        None,
    )


def build_restore_report(
    plan: RestorePlan,
    final: InventoryIndex,
    journal: RestoreJournal,
) -> RestoreReport:
    rows: list[ReportItem] = []
    manual = {
        entry.key for entry in journal.operations if entry.state is JournalState.MANUAL
    }
    for expected in plan.expected.items:
        operation = _operation_for(plan, expected)
        actual = final.find(expected.provider, expected.scope, expected.item)
        if operation is not None and operation.key in manual:
            rows.append(
                ReportItem(
                    ReportClass.MANUAL_ACTION,
                    expected.provider,
                    expected.item,
                    operation.manual_reason or "manual action required",
                )
            )
        elif actual is None:
            rows.append(
                ReportItem(
                    ReportClass.MISSING,
                    expected.provider,
                    expected.item,
                    "required item is missing",
                )
            )
        elif expected.version and actual.version and expected.version != actual.version:
            rows.append(
                ReportItem(
                    ReportClass.VERSION_DRIFT,
                    expected.provider,
                    expected.item,
                    f"expected {expected.version}; found {actual.version}",
                )
            )
        elif operation is not None or expected.provider is Provider.APPLICATION:
            rows.append(
                ReportItem(
                    ReportClass.RESTORED,
                    expected.provider,
                    expected.item,
                    "already present" if operation is None else "present after restore",
                )
            )
    expected_keys = {
        (row.provider, row.scope, row.item.casefold()) for row in plan.expected.items
    }
    for actual in final.items:
        if (actual.provider, actual.scope, actual.item.casefold()) not in expected_keys:
            rows.append(
                ReportItem(
                    ReportClass.EXTRA,
                    actual.provider,
                    actual.item,
                    "preserved extra item",
                )
            )
    for operation in plan.operations:
        if (
            operation.provider is Provider.ROUTINE
            and journal.find(operation.key).state is JournalState.SUCCEEDED
        ):
            rows.append(
                ReportItem(
                    ReportClass.RESTORED,
                    operation.provider,
                    operation.item,
                    "restore routine completed",
                )
            )
    failed = any(entry.state is JournalState.FAILED for entry in journal.operations)
    missing = any(row.classification is ReportClass.MISSING for row in rows)
    return RestoreReport(tuple(rows), 1 if failed or missing else 0)
