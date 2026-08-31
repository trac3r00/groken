from __future__ import annotations

import json
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest

from groken import bot_update
from groken.bot_update import (
    LocalEnvironmentSnapshots,
    ManifestUnavailableError,
    RestorePlan,
    RoutineFailedError,
    UpdateAvailability,
    UpdateKind,
    UpdateManifest,
    UpdateOptions,
    UpdateReadinessError,
    UpdateRuntime,
    run_update,
)
from groken.env_manifest import CaptureOutcome
from groken.env_restore import RestoreReport
from groken.env_restore_inventory import InventoryIndex
from groken.routines import RoutineEvent

NOW = datetime(2026, 8, 26, 12, tzinfo=UTC)


class FakeConsole:
    def __init__(self, answer: str | None = None) -> None:
        self.answer: str | None = answer
        self.lines: list[str] = []
        self.prompts: list[str] = []

    def write(self, line: str) -> None:
        self.lines.append(line)

    def prompt(self, message: str) -> str | None:
        self.prompts.append(message)
        return self.answer


class FakeSnapshots:
    def __init__(self, log: list[str]) -> None:
        self.log: list[str] = log

    def ensure(self, bot_id: str, *, skip_capture: bool) -> UpdateManifest:
        self.log.append(f"manifest:{bot_id}:{skip_capture}")
        return UpdateManifest("sha256:fixture", NOW, Path("/manifest"))


class FakeRoutines:
    def __init__(self, log: list[str], failing: RoutineEvent | None = None) -> None:
        self.log: list[str] = log
        self.failing: RoutineEvent | None = failing

    def run(self, event: RoutineEvent) -> tuple[str, ...]:
        self.log.append(event.value)
        if event is self.failing:
            raise RoutineFailedError("broken", event, 7)
        return (f"hook-{event.value}",)


class FakeRestore:
    def __init__(self, log: list[str]) -> None:
        self.log: list[str] = log
        self.restore_count: int = 0

    def plan(self, bot_id: str, manifest: UpdateManifest) -> RestorePlan:
        self.log.append(f"plan:{bot_id}:{manifest.manifest_id}")
        return RestorePlan(
            InventoryIndex(()),
            (),
            "restore packages=3 applications=1",
        )

    def restore(self, bot_id: str, manifest: UpdateManifest) -> RestoreReport:
        self.restore_count += 1
        self.log.append(f"restore:{bot_id}:{manifest.manifest_id}")
        return RestoreReport((), 0)


class FakeBackend:
    def __init__(
        self,
        log: list[str],
        readiness_error: UpdateReadinessError | None = None,
    ) -> None:
        self.log: list[str] = log
        self.readiness_error: UpdateReadinessError | None = readiness_error
        self.mutations: int = 0

    def resolve(self, bot: str | None) -> str:
        self.log.append(f"resolve:{bot}")
        return "bot-1"

    def availability(self, bot_id: str) -> UpdateAvailability:
        self.log.append(f"availability:{bot_id}")
        return UpdateAvailability(host=False, image=True)

    @contextmanager
    def subscribe(self, bot_id: str, timeout_s: float) -> Generator[None, None, None]:
        self.log.append(f"subscribe:{bot_id}:{timeout_s}")
        yield

    def trigger(self, bot_id: str, kind: UpdateKind) -> None:
        self.mutations += 1
        self.log.append(f"trigger:{bot_id}:{kind.value}")

    def wait_ready(self, bot_id: str) -> None:
        self.log.append(f"ready:{bot_id}")
        if self.readiness_error is not None:
            raise self.readiness_error


def runtime(
    log: list[str], backend: FakeBackend | None = None
) -> tuple[UpdateRuntime, FakeRestore]:
    restore = FakeRestore(log)
    return UpdateRuntime(
        backend or FakeBackend(log), FakeSnapshots(log), FakeRoutines(log), restore
    ), restore


def write_manifest(root: Path, captured_at: datetime, *, corrupt: bool = False) -> None:
    manifest_id = "sha256:" + "a" * 64
    bot_root = root / "bot-1"
    target = bot_root / manifest_id
    target.mkdir(parents=True, exist_ok=True)
    _ = (bot_root / "current.json").write_text(json.dumps({"manifest_id": manifest_id}))
    payload = (
        "{"
        if corrupt
        else json.dumps(
            {
                "schema_version": 1,
                "manifest_id": manifest_id,
                "bot": {"id": "bot-1", "name": "Demo"},
                "captured_at": captured_at.isoformat().replace("+00:00", "Z"),
            }
        )
    )
    _ = (target / "manifest.json").write_text(payload)


def test_update_orders_capture_availability_hooks_subscription_trigger_ready_and_plan() -> (
    None
):
    # Given
    log: list[str] = []
    services, restore = runtime(log)
    console = FakeConsole()

    # When
    run_update(UpdateOptions("Demo", yes=False, skip_capture=False), services, console)

    # Then
    assert log == [
        "resolve:Demo",
        "availability:bot-1",
        "manifest:bot-1:False",
        "pre-update",
        "subscribe:bot-1:600.0",
        "trigger:bot-1:image",
        "ready:bot-1",
        "post-update",
        "plan:bot-1:sha256:fixture",
    ]
    assert cast(FakeBackend, services.backend).mutations == 1
    assert restore.restore_count == 0
    assert json.loads(console.lines[0]) == {
        "bot": "bot-1",
        "hostUpdateAvailable": False,
        "imageUpdateAvailable": True,
        "selectedUpdate": "image",
    }
    assert console.lines[-2:] == [
        "restore packages=3 applications=1",
        "restore=skipped",
    ]


@pytest.mark.parametrize(
    ("answer", "expected"),
    [(None, 0), ("yes", 0), ("GO", 0), ("go\nrm", 0), ("go", 1), (" go \n", 1)],
)
def test_confirmation_accepts_only_literal_go(
    answer: str | None, expected: int
) -> None:
    # Given
    log: list[str] = []
    services, restore = runtime(log)

    # When
    run_update(
        UpdateOptions(None, yes=False, skip_capture=False),
        services,
        FakeConsole(answer),
    )

    # Then
    assert restore.restore_count == expected


def test_yes_restores_exactly_once_without_prompt() -> None:
    # Given
    log: list[str] = []
    services, restore = runtime(log)
    console = FakeConsole("not-read")

    # When
    run_update(UpdateOptions(None, yes=True, skip_capture=False), services, console)

    # Then
    assert restore.restore_count == 1
    assert console.prompts == []


@pytest.mark.parametrize("failure", [RoutineEvent.PRE_UPDATE, RoutineEvent.POST_UPDATE])
def test_routine_failure_identifies_hook_and_stops_later_work(
    failure: RoutineEvent,
) -> None:
    # Given
    log: list[str] = []
    backend = FakeBackend(log)
    restore = FakeRestore(log)
    services = UpdateRuntime(
        backend, FakeSnapshots(log), FakeRoutines(log, failure), restore
    )

    # When / Then
    with pytest.raises(RoutineFailedError, match=f"broken.*{failure.value}"):
        run_update(
            UpdateOptions(None, yes=True, skip_capture=False), services, FakeConsole()
        )
    assert backend.mutations == (0 if failure is RoutineEvent.PRE_UPDATE else 1)
    assert restore.restore_count == 0


@pytest.mark.parametrize(
    "captured_age", [timedelta(hours=23), timedelta(hours=24), timedelta(hours=25)]
)
def test_manifest_freshness_captures_only_when_older_than_24h(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    captured_age: timedelta,
) -> None:
    # Given
    write_manifest(tmp_path, NOW - captured_age)
    captures = 0

    def capture(_manager: bot_update.UpdateGateway, _bot: str | None) -> CaptureOutcome:
        nonlocal captures
        captures += 1
        write_manifest(tmp_path, NOW)
        return CaptureOutcome("sha256:" + "a" * 64, tmp_path)

    monkeypatch.setattr(bot_update, "capture_for_gateway", capture)
    snapshots = LocalEnvironmentSnapshots(
        cast(bot_update.UpdateGateway, object()), tmp_path, lambda: NOW
    )

    # When
    manifest = snapshots.ensure("bot-1", skip_capture=False)

    # Then
    assert manifest.captured_at == (
        NOW if captured_age > timedelta(hours=24) else NOW - captured_age
    )
    assert captures == (1 if captured_age > timedelta(hours=24) else 0)


@pytest.mark.parametrize("state", ["absent", "corrupt"])
def test_skip_capture_without_usable_manifest_fails_before_mutation(
    tmp_path: Path,
    state: str,
) -> None:
    # Given
    if state == "corrupt":
        write_manifest(tmp_path, NOW, corrupt=True)
    log: list[str] = []
    backend = FakeBackend(log)
    snapshots = LocalEnvironmentSnapshots(
        cast(bot_update.UpdateGateway, object()), tmp_path, lambda: NOW
    )
    services = UpdateRuntime(backend, snapshots, FakeRoutines(log), FakeRestore(log))

    # When / Then
    with pytest.raises(ManifestUnavailableError, match="capture"):
        run_update(
            UpdateOptions(None, yes=True, skip_capture=True), services, FakeConsole()
        )
    assert backend.mutations == 0
