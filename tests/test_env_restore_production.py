from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import final

import pytest

from groken.bot_update import RestoreUnavailableError
from groken.env_collectors import Inventory
from groken.env_native_runner import CapturePhase
from groken.env_restore import RestoreRunRequest, RestoreRunResult
from groken.env_restore_gateway import (
    GatewayRestoreDependencies,
    GatewayRestoreService,
    RestoreCommandOptions,
    run_restore_command,
)
from groken.env_restore_manifest import JsonValue, LoadedInventory

MANIFEST_ID = "sha256:" + "6" * 64


def inventory(*, installed: bool) -> Inventory:
    return Inventory(
        "",
        (),
        {
            "node_version": "v24",
            "prefix": "/usr/local",
            "packages": (
                [{"name": "typescript", "version": "5.9.2"}] if installed else []
            ),
        },
        (),
        (),
        (),
    )


def write_current(root: Path) -> None:
    path = root / "bot-1" / MANIFEST_ID
    path.mkdir(parents=True)
    _ = (path.parent / "current.json").write_text(
        json.dumps({"manifest_id": MANIFEST_ID})
    )
    payload: dict[str, JsonValue] = {
        "schema_version": 1,
        "manifest_id": MANIFEST_ID,
        "bot": {"id": "bot-1", "name": "Demo"},
        "captured_at": "2026-08-26T12:00:00Z",
        "host": {"os": "Darwin", "os_version": "25", "arch": "arm64"},
        "collectors": [],
        "inventory": {
            "brewfile": "",
            "python": [],
            "npm": {
                "node_version": "v24",
                "prefix": "/usr/local",
                "packages": [
                    {"name": "typescript", "version": "5.9.2"},
                ],
            },
            "pipx": [],
            "mas": [],
            "applications": [],
        },
    }
    _ = (path / "manifest.json").write_text(json.dumps(payload))


@final
class Gateway:
    def resolve_agent(self, bot: str | None = None) -> str:
        return bot or "bot-1"

    def ask(self, agent_id: str, text: str, timeout_s: float = 600) -> str:
        raise AssertionError((agent_id, text, timeout_s))


@final
class Environment:
    def __init__(self) -> None:
        self.installed = False
        self.capture_phases: list[CapturePhase] = []
        self.restore_requests: list[RestoreRunRequest] = []
        self.prepared: list[LoadedInventory] = []
        self.closed = False

    def capture(self, manifest_id: str, phase: CapturePhase) -> Inventory:
        assert manifest_id == MANIFEST_ID
        self.capture_phases.append(phase)
        return inventory(installed=self.installed)

    def brewfile_path(self, loaded: LoadedInventory) -> Path | None:
        return loaded.brewfile_path

    def prepare(self, loaded: LoadedInventory) -> None:
        self.prepared.append(loaded)

    def run_restore(self, request: RestoreRunRequest) -> RestoreRunResult:
        self.restore_requests.append(request)
        self.installed = True
        return RestoreRunResult(request.argv, 0, b"ok", b"", False, False, None)

    def close(self) -> None:
        self.closed = True


@final
class Console:
    def __init__(self, answer: str | None) -> None:
        self.answer = answer
        self.lines: list[str] = []

    def write(self, line: str) -> None:
        self.lines.append(line)

    def prompt(self, message: str) -> str | None:
        del message
        return self.answer


def service(root: Path, environment: Environment) -> GatewayRestoreService:
    dependencies = GatewayRestoreDependencies(
        root=root,
        routines=lambda: (),
        environment_factory=lambda: environment,
        now=lambda: datetime.now(UTC),
    )
    return GatewayRestoreService(Gateway(), dependencies)


def test_direct_production_service_confirms_then_restores_and_recaptures(
    tmp_path: Path,
) -> None:
    # Given
    root = tmp_path / "env"
    write_current(root)
    environment = Environment()
    restore = service(root, environment)

    # When
    run_restore_command(
        RestoreCommandOptions("bot-1", True, False),
        restore,
        Console(None),
    )
    restore.close()

    # Then
    assert environment.capture_phases == [
        CapturePhase.PLAN,
        CapturePhase.PRE_RESTORE,
        CapturePhase.POST_RESTORE,
    ]
    assert len(environment.restore_requests) == 1
    assert environment.restore_requests[0].argv == (
        "/usr/bin/env",
        "npm",
        "install",
        "--global",
        "typescript@5.9.2",
    )
    assert environment.prepared and environment.closed
    assert restore.report is not None and restore.report.exit_code == 0


def test_direct_production_denial_runs_zero_restore_operations(tmp_path: Path) -> None:
    # Given
    root = tmp_path / "env"
    write_current(root)
    environment = Environment()
    restore = service(root, environment)

    # When
    run_restore_command(
        RestoreCommandOptions("bot-1", False, False),
        restore,
        Console(None),
    )
    restore.close()

    # Then
    assert environment.capture_phases == [CapturePhase.PLAN]
    assert environment.restore_requests == []
    assert environment.prepared == []


def test_missing_native_configuration_is_precise_restore_error(tmp_path: Path) -> None:
    # Given
    root = tmp_path / "env"
    write_current(root)

    def unavailable() -> Environment:
        raise RestoreUnavailableError(
            "native restore unavailable: controller token is missing"
        )

    dependencies = GatewayRestoreDependencies(
        root=root,
        routines=lambda: (),
        environment_factory=unavailable,
        now=lambda: datetime.now(UTC),
    )
    restore = GatewayRestoreService(Gateway(), dependencies)

    # When / Then
    with pytest.raises(RestoreUnavailableError, match="controller token"):
        _ = restore.plan("bot-1", restore.manifest("bot-1"))
