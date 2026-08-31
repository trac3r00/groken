from __future__ import annotations

import base64
from pathlib import Path
from typing import final

import httpx
import pytest

from groken.env_collectors import Inventory
from groken.env_native_runner import NativeEnvironmentRunner, NativeEnvironmentSettings
from groken.env_restore import (
    RestoreContext,
    RestoreOptions,
    RestorePendingError,
    RestoreRequest,
    RestoreRunRequest,
    execute_restore,
    plan_restore,
)
from groken.env_restore_store import JournalState, JournalStore
from groken.native_client import NativeControllerClient
from groken.native_models import NativeOperationRequest, TerminalExec

MANIFEST_ID = "sha256:" + "7" * 64


def inventory(*, npm: bool = False) -> Inventory:
    return Inventory(
        "",
        (),
        {
            "node_version": "v24",
            "prefix": "/usr/local",
            "packages": ([{"name": "typescript", "version": "5.9.2"}] if npm else []),
        },
        (),
        (),
        (),
    )


@final
class ScriptedController:
    def __init__(self) -> None:
        self.requests: dict[str, NativeOperationRequest] = {}
        self.keys: list[str] = []
        self.disconnects = 0
        self.terminal_failures = 0
        self.signal: int | None = None
        self.wait_timeout = False

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            key = request.headers["idempotency-key"]
            operation = NativeOperationRequest.model_validate_json(request.content)
            prior = self.requests.get(key)
            if prior is not None and prior != operation:
                return httpx.Response(409, json={"detail": "idempotency conflict"})
            self.requests[key] = operation
            self.keys.append(key)
            return httpx.Response(202, json={"operation_id": key, "status": "queued"})
        operation_id = request.url.path.split("/")[-2]
        operation = self.requests[operation_id]
        if self.disconnects:
            self.disconnects -= 1
            raise httpx.ReadTimeout("ambiguous wait disconnect", request=request)
        if self.wait_timeout:
            return httpx.Response(
                408,
                json={
                    "operation_id": operation_id,
                    "status": "timeout",
                    "timeout_s": 35.0,
                },
            )
        status = "completed"
        result = {
            "type": "terminal.exec",
            "exit_code": 0,
            "stdout_b64": base64.b64encode(b"out").decode(),
            "stderr_b64": base64.b64encode(b"err").decode(),
            "timed_out": False,
            "truncated": False,
            "signal": self.signal,
        }
        error = None
        if self.terminal_failures:
            self.terminal_failures -= 1
            status, result, error = "failed", None, "worker failed"
        return httpx.Response(
            200,
            json={
                "operation_id": operation_id,
                "status": status,
                "target": operation.target,
                "workspace": operation.workspace,
                "operation": operation.operation.model_dump(mode="json"),
                "result": result,
                "error": error,
            },
        )


def environment(
    script: ScriptedController,
) -> tuple[NativeEnvironmentRunner, httpx.Client]:
    client = httpx.Client(
        transport=httpx.MockTransport(script.handle),
        base_url="http://controller.test",
    )
    native = NativeControllerClient(token="test-token", client=client)
    return NativeEnvironmentRunner(native, NativeEnvironmentSettings()), client


def test_restore_maps_exact_terminal_exec_and_native_result() -> None:
    # Given
    script = ScriptedController()
    script.signal = 15
    runner, client = environment(script)
    run = RestoreRunRequest(
        manifest_id=MANIFEST_ID,
        operation_key="restore/npm/global/typescript",
        attempt=1,
        idempotency_key="restore-key",
        argv=("/usr/bin/env", "npm", "install", "--global", "typescript@5.9.2"),
        stdin=b"literal-input",
        timeout_ms=45_000,
    )

    # When
    try:
        result = runner.run_restore(run)
    finally:
        runner.close()
        client.close()

    # Then
    request = script.requests["restore-key"]
    assert request.target == "groken-box"
    assert request.workspace == "native"
    assert request.operation.type == "terminal.exec"
    assert request.operation.argv == list(run.argv)
    assert request.operation.stdin_b64 == base64.b64encode(run.stdin).decode()
    assert request.operation.timeout_ms == 45_000
    assert result.argv == run.argv
    assert result.stdout == b"out" and result.stderr == b"err"
    assert result.exit_code == 0 and result.signal == 15
    assert result.timed_out is False and result.truncated is False


def test_restore_ambiguous_wait_reuses_persisted_attempt_key(tmp_path: Path) -> None:
    # Given
    script = ScriptedController()
    script.disconnects = 1
    runner, client = environment(script)
    expected = inventory(npm=True)
    plan = plan_restore(RestoreRequest(expected, inventory(), None, ()))
    captures = iter((inventory(), inventory(), expected))
    store = JournalStore(tmp_path / "env", "bot-1", MANIFEST_ID)
    context = RestoreContext(store, runner, lambda: next(captures), RestoreOptions())

    # When
    try:
        with pytest.raises(httpx.ReadTimeout, match="ambiguous"):
            _ = execute_restore(plan, context)
        interrupted = store.load()
        report = execute_restore(plan, context)
    finally:
        runner.close()
        client.close()

    # Then
    assert interrupted is not None
    assert interrupted.operations[0].state is JournalState.RUNNING
    assert interrupted.operations[0].attempts == 1
    persisted_key = interrupted.operations[0].idempotency_key
    assert persisted_key is not None
    assert script.keys == [persisted_key, persisted_key]
    submitted = TerminalExec.model_validate(
        script.requests[persisted_key].operation.model_dump(),
    )
    assert submitted.argv == list(plan.operations[0].argv)
    assert report.exit_code == 0


def test_terminal_failure_retry_increments_attempt_and_key(tmp_path: Path) -> None:
    # Given
    script = ScriptedController()
    script.terminal_failures = 1
    runner, client = environment(script)
    expected = inventory(npm=True)
    plan = plan_restore(RestoreRequest(expected, inventory(), None, ()))
    captures = iter((inventory(), inventory(), inventory(), expected))
    store = JournalStore(tmp_path / "env", "bot-1", MANIFEST_ID)
    context = RestoreContext(store, runner, lambda: next(captures), RestoreOptions())

    # When
    try:
        failed = execute_restore(plan, context)
        first = store.load()
        restored = execute_restore(plan, context)
    finally:
        runner.close()
        client.close()

    # Then
    assert failed.exit_code == 1 and restored.exit_code == 0
    assert first is not None and first.operations[0].state is JournalState.FAILED
    assert first.operations[0].attempts == 1
    final = store.load()
    assert final is not None and final.operations[0].attempts == 2
    assert len(script.keys) == 2 and script.keys[0] != script.keys[1]


def test_wait_timeout_is_actionable_pending() -> None:
    # Given
    script = ScriptedController()
    script.wait_timeout = True
    runner, client = environment(script)
    request = RestoreRunRequest(
        MANIFEST_ID,
        "restore/npm/global/typescript",
        1,
        "typed-failure",
        ("/usr/bin/env", "npm", "install", "--global", "typescript@5.9.2"),
        b"",
        30_000,
    )

    # When / Then
    try:
        with pytest.raises(RestorePendingError, match="pending") as pending:
            _ = runner.run_restore(request)
    finally:
        runner.close()
        client.close()
    assert "failed" not in str(pending.value).casefold()
