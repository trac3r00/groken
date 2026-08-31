from __future__ import annotations

import json
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import final

from groken.bot_update import (
    UpdateAvailability,
    UpdateKind,
    UpdateManifest,
    UpdateOptions,
    UpdateRuntime,
    run_update,
)
from groken.env_collectors import Inventory
from groken.env_native_runner import CapturePhase
from groken.env_restore import ReportClass, RestoreRunRequest, RestoreRunResult
from groken.env_restore_gateway import GatewayRestoreDependencies, GatewayRestoreService
from groken.env_restore_manifest import JsonValue, LoadedInventory
from groken.env_restore_service import NativeRestoreRuntime, NativeRestoreService
from groken.routines import RoutineEvent

MANIFEST_ID = "sha256:" + "e" * 64


def inventory(*, installed: bool) -> Inventory:
    return Inventory(
        'brew "jq"\n' if installed else "",
        (),
        {"node_version": "", "prefix": "", "packages": []},
        (),
        (),
        (),
    )


def write_manifest(root: Path) -> UpdateManifest:
    path = root / MANIFEST_ID
    path.mkdir(parents=True)
    payload: dict[str, JsonValue] = {
        "schema_version": 1,
        "manifest_id": MANIFEST_ID,
        "bot": {"id": "bot-1", "name": "Demo"},
        "captured_at": "2026-08-26T12:00:00Z",
        "host": {"os": "Darwin", "os_version": "25", "arch": "arm64"},
        "collectors": [
            {
                "id": "brew",
                "status": "ok",
                "artifact": "artifacts/brew.raw",
                "sha256": "fixture",
                "command": ["brew"],
                "exit_code": 0,
                "error": None,
            }
        ],
        "inventory": {
            "brewfile": 'brew "jq"\n',
            "python": [],
            "npm": {
                "node_version": "",
                "prefix": "",
                "packages": [],
            },
            "pipx": [],
            "mas": [],
            "applications": [],
        },
    }
    artifact = path / "artifacts" / "brew.raw"
    artifact.parent.mkdir()
    _ = artifact.write_text('brew "jq"\n')
    _ = (path / "manifest.json").write_text(json.dumps(payload))
    return UpdateManifest(MANIFEST_ID, datetime.now(UTC), path)


@final
class Runner:
    def __init__(self) -> None:
        self.installed = False
        self.requests: list[RestoreRunRequest] = []

    def run_restore(self, request: RestoreRunRequest) -> RestoreRunResult:
        self.requests.append(request)
        self.installed = True
        return RestoreRunResult(
            request.argv,
            0,
            b"installed",
            b"",
            False,
            False,
            None,
        )

    def capture(self, manifest_id: str, phase: CapturePhase) -> Inventory:
        del manifest_id, phase
        return inventory(installed=self.installed)

    def brewfile_path(self, loaded: LoadedInventory) -> Path | None:
        return loaded.brewfile_path

    def prepare(self, loaded: LoadedInventory) -> None:
        del loaded

    def close(self) -> None:
        return None


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
    def __init__(self, manifest: UpdateManifest) -> None:
        self._manifest = manifest

    def ensure(self, bot_id: str, *, skip_capture: bool) -> UpdateManifest:
        del bot_id, skip_capture
        return self._manifest


@final
class Routines:
    def run(self, event: RoutineEvent) -> tuple[str, ...]:
        del event
        return ()


@final
class Console:
    def __init__(self, answer: str | None) -> None:
        self._answer = answer
        self.lines: list[str] = []
        self.prompts = 0

    def write(self, line: str) -> None:
        self.lines.append(line)

    def prompt(self, message: str) -> str | None:
        del message
        self.prompts += 1
        return self._answer


def service(
    tmp_path: Path, runner: Runner
) -> tuple[NativeRestoreService, UpdateManifest]:
    manifest = write_manifest(tmp_path / "manifest")
    runtime = NativeRestoreRuntime(
        tmp_path / "env",
        runner,
        lambda: (),
        lambda: datetime.now(UTC),
    )
    return NativeRestoreService(runtime), manifest


def test_task5_yes_invokes_real_native_restore_seam_once(tmp_path: Path) -> None:
    # Given
    runner = Runner()
    restore, manifest = service(tmp_path, runner)
    runtime = UpdateRuntime(Backend(), Snapshots(manifest), Routines(), restore)
    console = Console(None)

    # When
    run_update(UpdateOptions("bot-1", True, False), runtime, console)

    # Then
    assert [request.argv for request in runner.requests] == [
        (
            "/usr/bin/env",
            "brew",
            "bundle",
            "--file",
            str(manifest.path / "artifacts" / "brew.raw"),
        )
    ]
    assert restore.report is not None
    assert restore.report.count(ReportClass.RESTORED) == 1
    assert console.prompts == 0


@final
class Gateway:
    def resolve_agent(self, bot: str | None = None) -> str:
        return bot or "bot-1"


def test_task5_production_service_uses_native_environment_once(tmp_path: Path) -> None:
    # Given
    runner = Runner()
    manifest = write_manifest(tmp_path / "manifest")
    dependencies = GatewayRestoreDependencies(
        tmp_path / "env",
        lambda: (),
        lambda: runner,
        lambda: datetime.now(UTC),
    )
    restore = GatewayRestoreService(Gateway(), dependencies)
    runtime = UpdateRuntime(Backend(), Snapshots(manifest), Routines(), restore)

    # When
    try:
        run_update(UpdateOptions("bot-1", True, False), runtime, Console(None))
    finally:
        restore.close()

    # Then
    assert len(runner.requests) == 1
    assert restore.report is not None and restore.report.exit_code == 0


def test_task5_default_denial_does_not_invoke_native_restore(tmp_path: Path) -> None:
    # Given
    runner = Runner()
    restore, manifest = service(tmp_path, runner)
    runtime = UpdateRuntime(Backend(), Snapshots(manifest), Routines(), restore)

    # When
    run_update(UpdateOptions("bot-1", False, False), runtime, Console(None))

    # Then
    assert runner.requests == []
    assert restore.report is None
