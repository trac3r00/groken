from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Never, final

import pytest

from groken import cli
from groken.bot_update import RestorePlan, RestoreUnavailableError, UpdateManifest
from groken.env_restore import RestoreReport
from groken.env_restore_gateway import (
    GatewayRestoreDependencies,
    GatewayRestoreService,
    RestoreCommandOptions,
    run_restore_command,
)
from groken.env_restore_inventory import InventoryIndex
from groken.env_restore_manifest import JsonValue

MANIFEST_ID = "sha256:" + "d" * 64


@final
class Console:
    def __init__(self, answer: str | None) -> None:
        self.answer: str | None = answer
        self.lines: list[str] = []
        self.prompts: list[str] = []

    def write(self, line: str) -> None:
        self.lines.append(line)

    def prompt(self, message: str) -> str | None:
        self.prompts.append(message)
        return self.answer


@final
class Service:
    def __init__(self) -> None:
        self.restores: list[tuple[str, bool]] = []

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
            "[brew]\n- restore/brew/system/jq",
        )

    def restore(
        self,
        bot_id: str,
        manifest: UpdateManifest,
        *,
        retry_manual: bool = False,
    ) -> RestoreReport:
        del manifest
        self.restores.append((bot_id, retry_manual))
        return RestoreReport((), 0)


def test_restore_command_prints_plan_and_requires_trimmed_lowercase_go() -> None:
    # Given
    service = Service()

    # When
    denied = Console(None)
    run_restore_command(RestoreCommandOptions(None, False, False), service, denied)
    wrong = Console("GO")
    run_restore_command(RestoreCommandOptions(None, False, False), service, wrong)
    accepted = Console(" go \n")
    run_restore_command(RestoreCommandOptions(None, False, True), service, accepted)

    # Then
    assert service.restores == [("bot-1", True)]
    assert denied.lines == ["[brew]\n- restore/brew/system/jq", "restore=skipped"]
    assert len(denied.prompts) == 1


def test_cli_restore_dispatch_preserves_bot_yes_and_retry_manual(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    calls: list[tuple[str | None, bool, bool]] = []

    def restore(bot: str | None, *, yes: bool, retry_manual: bool) -> None:
        calls.append((bot, yes, retry_manual))

    monkeypatch.setattr(cli, "cmd_bot_env_restore", restore)
    monkeypatch.setattr(
        sys,
        "argv",
        ["groken", "bot", "env", "restore", "Demo", "--yes", "--retry-manual"],
    )

    # When
    cli.main()

    # Then
    assert calls == [("Demo", True, True)]


@final
class Gateway:
    def resolve_agent(self, bot: str | None = None) -> str:
        return bot or "bot-1"

    def ask(self, agent_id: str, text: str, timeout_s: float = 600) -> str:
        del agent_id, text, timeout_s
        return json.dumps(
            {
                "host": {"os": "Darwin", "os_version": "25", "arch": "arm64"},
                "inventory": {
                    "brewfile": "",
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
        )


def test_gateway_service_fails_precisely_without_native_configuration(
    tmp_path: Path,
) -> None:
    # Given
    manifest = tmp_path / MANIFEST_ID
    manifest.mkdir()
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
    artifact = manifest / "artifacts" / "brew.raw"
    artifact.parent.mkdir()
    _ = artifact.write_text('brew "jq"\n')
    _ = (manifest / "manifest.json").write_text(json.dumps(payload))

    def unavailable() -> Never:
        raise RestoreUnavailableError(
            "native restore unavailable: controller token missing"
        )

    dependencies = GatewayRestoreDependencies(
        tmp_path,
        lambda: (),
        unavailable,
        lambda: datetime.now(UTC),
    )
    service = GatewayRestoreService(Gateway(), dependencies)
    update_manifest = UpdateManifest(MANIFEST_ID, datetime.now(UTC), manifest)

    # When / Then
    with pytest.raises(RestoreUnavailableError, match="controller token"):
        _ = service.plan("bot-1", update_manifest)
