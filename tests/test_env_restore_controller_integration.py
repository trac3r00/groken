from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import final

import httpx

from groken.env_native_runner import NativeEnvironmentRunner
from groken.env_restore_gateway import (
    GatewayRestoreDependencies,
    GatewayRestoreService,
    RestoreCommandOptions,
    run_restore_command,
)
from groken.env_restore_manifest import JsonValue
from groken.native_client import NativeControllerClient
from groken.native_models import NativeOperationRequest, TerminalExec

MANIFEST_ID = "sha256:" + "6" * 64


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
class InventoryController:
    def __init__(self) -> None:
        self.requests: dict[str, NativeOperationRequest] = {}
        self.installed = False
        self.install_keys: list[str] = []

    def _result(self, operation: TerminalExec) -> tuple[int, bytes]:
        argv = tuple(operation.argv)
        if argv[:2] == ("/usr/bin/env", "npm"):
            self.installed = True
            return 0, b"installed"
        lookups: dict[tuple[str, ...], tuple[int, bytes]] = {
            ("/usr/bin/which", "npm"): (0, b"/usr/bin/npm\n"),
            ("/usr/bin/which", "node"): (0, b"/usr/bin/node\n"),
            ("/usr/bin/node", "--version"): (0, b"v24\n"),
            ("/usr/bin/npm", "prefix", "-g"): (0, b"/usr/local\n"),
        }
        known = lookups.get(argv)
        if known is not None:
            return known
        if argv == ("/usr/bin/npm", "-g", "list", "--depth=0", "--json"):
            packages = {"typescript": {"version": "5.9.2"}} if self.installed else {}
            return 0, json.dumps({"dependencies": packages}).encode()
        if argv[0] == "/usr/bin/which":
            return 1, b""
        if argv[0] in {"/usr/bin/uname", "/usr/bin/find"}:
            return 0, b"Darwin\n" if argv[0].endswith("uname") else b""
        return 1, b""

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            key = request.headers["idempotency-key"]
            operation = NativeOperationRequest.model_validate_json(request.content)
            self.requests[key] = operation
            terminal = TerminalExec.model_validate(operation.operation.model_dump())
            if tuple(terminal.argv[:2]) == ("/usr/bin/env", "npm"):
                self.install_keys.append(key)
            return httpx.Response(202, json={"operation_id": key, "status": "queued"})
        key = request.url.path.split("/")[-2]
        native_request = self.requests[key]
        operation = TerminalExec.model_validate(native_request.operation.model_dump())
        exit_code, stdout = self._result(operation)
        return httpx.Response(
            200,
            json={
                "operation_id": key,
                "status": "completed",
                "target": native_request.target,
                "workspace": native_request.workspace,
                "operation": operation.model_dump(mode="json"),
                "result": {
                    "type": "terminal.exec",
                    "exit_code": exit_code,
                    "stdout_b64": base64.b64encode(stdout).decode(),
                    "stderr_b64": "",
                    "timed_out": False,
                    "truncated": False,
                    "signal": None,
                },
                "error": None,
            },
        )


def test_direct_production_native_controller_runs_plan_drift_and_output(
    tmp_path: Path,
) -> None:
    root = tmp_path / "env"
    write_current(root)
    controller = InventoryController()
    http_client = httpx.Client(
        transport=httpx.MockTransport(controller.handle),
        base_url="http://controller.test",
    )
    environment = NativeEnvironmentRunner(
        NativeControllerClient(token="test-token", client=http_client),
    )
    dependencies = GatewayRestoreDependencies(
        root,
        lambda: (),
        lambda: environment,
        lambda: datetime.now(UTC),
    )
    restore = GatewayRestoreService(Gateway(), dependencies)
    console = Console()
    try:
        run_restore_command(
            RestoreCommandOptions("bot-1", True, False),
            restore,
            console,
        )
    finally:
        restore.close()
        http_client.close()
    assert len(controller.install_keys) == 1
    assert console.lines[-2:] == [
        "restore-report restored=1 version-drift=0 missing=0 extra=0 manual-action=0",
        f"restore=completed manifest_id={MANIFEST_ID}",
    ]
    operation = TerminalExec.model_validate(
        controller.requests[controller.install_keys[0]].operation.model_dump(),
    )
    assert tuple(operation.argv) == (
        "/usr/bin/env",
        "npm",
        "install",
        "--global",
        "typescript@5.9.2",
    )
