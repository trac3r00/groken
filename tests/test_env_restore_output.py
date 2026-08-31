from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import final

import pytest

from groken.bot_update import (
    UpdateAvailability,
    UpdateKind,
    UpdateManifest,
    UpdateOptions,
    UpdateRuntime,
    run_update,
)
from groken.env_restore import (
    ReportClass,
    ReportItem,
    RestorePendingError,
    RestoreReport,
)
from groken.env_restore_contracts import RestorePlan
from groken.env_restore_gateway import RestoreCommandOptions, run_restore_command
from groken.env_restore_inventory import InventoryIndex
from groken.env_restore_validation import Provider
from groken.routines import RoutineEvent

MANIFEST_ID = "sha256:" + "4" * 64
REPORT = RestoreReport(
    (
        ReportItem(ReportClass.RESTORED, Provider.NPM, "restored", "done"),
        ReportItem(ReportClass.VERSION_DRIFT, Provider.PIPX, "drift", "different"),
        ReportItem(ReportClass.MISSING, Provider.MAS, "missing", "absent"),
        ReportItem(ReportClass.EXTRA, Provider.BREW, "extra", "preserved"),
        ReportItem(ReportClass.MANUAL_ACTION, Provider.APPLICATION, "manual", "login"),
    ),
    0,
)


@final
class Console:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def write(self, line: str) -> None:
        self.lines.append(line)

    def prompt(self, message: str) -> str | None:
        del message
        return None


@final
class Restore:
    def __init__(self, *, pending: bool = False) -> None:
        self.pending = pending
        self.calls = 0

    def resolve(self, bot: str | None) -> str:
        return bot or "bot-1"

    def manifest(self, bot_id: str) -> UpdateManifest:
        del bot_id
        return UpdateManifest(MANIFEST_ID, datetime.now(UTC), Path("/manifest"))

    def plan(self, bot_id: str, manifest: UpdateManifest) -> RestorePlan:
        del bot_id, manifest
        return RestorePlan(
            InventoryIndex(()),
            (),
            "[npm]\n- restore/npm/global/typescript",
        )

    def restore(
        self,
        bot_id: str,
        manifest: UpdateManifest,
        *,
        retry_manual: bool = False,
    ) -> RestoreReport:
        del bot_id, manifest, retry_manual
        self.calls += 1
        if self.pending:
            raise RestorePendingError(
                "native restore pending after wait timeout; rerun to resume"
            )
        return REPORT


@final
class Backend:
    def resolve(self, bot: str | None) -> str:
        return bot or "bot-1"

    def availability(self, bot_id: str) -> UpdateAvailability:
        del bot_id
        return UpdateAvailability(False, True)

    @contextmanager
    def subscribe(self, bot_id: str, timeout_s: float) -> Generator[None, None, None]:
        del bot_id, timeout_s
        yield

    def trigger(self, bot_id: str, kind: UpdateKind) -> None:
        del bot_id
        assert kind is UpdateKind.IMAGE

    def wait_ready(self, bot_id: str) -> None:
        del bot_id


@final
class Snapshots:
    def ensure(self, bot_id: str, *, skip_capture: bool) -> UpdateManifest:
        del bot_id, skip_capture
        return UpdateManifest(MANIFEST_ID, datetime.now(UTC), Path("/manifest"))


@final
class Routines:
    def run(self, event: RoutineEvent) -> tuple[str, ...]:
        del event
        return ()


def test_direct_restore_prints_drift_counts_and_manifest_completion() -> None:
    # Given
    console = Console()
    restore = Restore()

    # When
    run_restore_command(
        RestoreCommandOptions("bot-1", True, False),
        restore,
        console,
    )

    # Then
    assert console.lines[-2:] == [
        "restore-report restored=1 version-drift=1 missing=1 extra=1 manual-action=1",
        f"restore=completed manifest_id={MANIFEST_ID}",
    ]
    assert restore.calls == 1


def test_task5_update_prints_restore_result_once() -> None:
    # Given
    console = Console()
    restore = Restore()
    runtime = UpdateRuntime(Backend(), Snapshots(), Routines(), restore)

    # When
    run_update(UpdateOptions("bot-1", True, False), runtime, console)

    # Then
    assert console.lines.count(REPORT.summary) == 1
    assert console.lines.count(f"restore=completed manifest_id={MANIFEST_ID}") == 1


def test_pending_restore_never_prints_complete() -> None:
    # Given
    console = Console()
    restore = Restore(pending=True)

    # When / Then
    with pytest.raises(RestorePendingError, match="pending"):
        run_restore_command(
            RestoreCommandOptions("bot-1", True, False),
            restore,
            console,
        )
    assert REPORT.summary not in console.lines
    assert not any(line.startswith("restore=completed") for line in console.lines)
