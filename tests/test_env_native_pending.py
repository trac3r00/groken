from __future__ import annotations

import base64
from pathlib import Path
from typing import final

import httpx
import pytest

from groken.env_collectors import Inventory
from groken.env_native_runner import NativeEnvironmentRunner
from groken.env_restore import (
    RestoreContext,
    RestoreOptions,
    RestorePendingError,
    RestoreRequest,
    execute_restore,
    plan_restore,
)
from groken.env_restore_store import JournalState, JournalStore
from groken.native_client import NativeControllerClient
from groken.native_models import NativeOperationRequest

MANIFEST_ID = "sha256:" + "5" * 64


def inventory(*, installed: bool = False) -> Inventory:
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


class ProcessInterrupted(BaseException):
    """Simulated process death immediately after submit and before wait completion."""


@final
class PendingController:
    def __init__(
        self,
        *,
        interrupt: bool = False,
        malformed_result: str | None = None,
    ) -> None:
        self.requests: dict[str, NativeOperationRequest] = {}
        self.keys: list[str] = []
        self.bodies: list[bytes] = []
        self.pending = not interrupt and malformed_result is None
        self.interrupt = interrupt
        self.malformed_result = malformed_result

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            key = request.headers["idempotency-key"]
            operation = NativeOperationRequest.model_validate_json(request.content)
            prior = self.requests.get(key)
            if prior is not None and prior != operation:
                return httpx.Response(409, json={"detail": "idempotency conflict"})
            self.requests[key] = operation
            self.keys.append(key)
            self.bodies.append(request.content)
            return httpx.Response(202, json={"operation_id": key, "status": "queued"})
        key = request.url.path.split("/")[-2]
        if self.interrupt:
            self.interrupt = False
            raise ProcessInterrupted
        if self.pending:
            self.pending = False
            return httpx.Response(
                408,
                json={
                    "operation_id": key,
                    "status": "timeout",
                    "timeout_s": 35.0,
                },
            )
        operation = self.requests[key]
        result: dict[str, str | int | bool | None] | list[str] = {
            "type": "terminal.exec",
            "exit_code": 0,
            "stdout_b64": base64.b64encode(b"ok").decode(),
            "stderr_b64": "",
            "timed_out": False,
            "truncated": False,
            "signal": None,
        }
        if self.malformed_result == "missing-field":
            del result["stderr_b64"]
        if self.malformed_result == "wrong-shape":
            result = []
        return httpx.Response(
            200,
            json={
                "operation_id": key,
                "status": "completed",
                "target": operation.target,
                "workspace": operation.workspace,
                "operation": operation.operation.model_dump(mode="json"),
                "result": result,
                "error": None,
            },
        )


def environment(
    controller: PendingController,
) -> tuple[NativeEnvironmentRunner, httpx.Client]:
    http_client = httpx.Client(
        transport=httpx.MockTransport(controller.handle),
        base_url="http://controller.test",
    )
    native = NativeControllerClient(token="test-token", client=http_client)
    return NativeEnvironmentRunner(native), http_client


def context(
    tmp_path: Path,
    runner: NativeEnvironmentRunner,
) -> tuple[RestoreContext, JournalStore]:
    captures = iter((inventory(), inventory(), inventory(installed=True)))
    store = JournalStore(tmp_path / "env", "bot-1", MANIFEST_ID)
    return RestoreContext(
        store, runner, lambda: next(captures), RestoreOptions()
    ), store


def test_wait_timeout_stays_running_and_resumes_same_key_body(tmp_path: Path) -> None:
    # Given
    controller = PendingController()
    runner, client = environment(controller)
    plan = plan_restore(
        RestoreRequest(inventory(installed=True), inventory(), None, ())
    )
    restore_context, store = context(tmp_path, runner)

    # When
    try:
        with pytest.raises(RestorePendingError, match="pending|timeout") as pending:
            _ = execute_restore(plan, restore_context)
        interrupted = store.load()
        report = execute_restore(plan, restore_context)
    finally:
        runner.close()
        client.close()

    # Then
    assert "failed" not in str(pending.value).casefold()
    assert interrupted is not None
    assert interrupted.operations[0].state is JournalState.RUNNING
    assert interrupted.operations[0].attempts == 1
    assert controller.keys == [interrupted.operations[0].idempotency_key] * 2
    assert len(set(controller.keys)) == 1
    assert controller.bodies[0] == controller.bodies[1]
    assert report.exit_code == 0
    final = store.load()
    assert final is not None and final.operations[0].state is JournalState.SUCCEEDED
    assert final.operations[0].attempts == 1


@pytest.mark.parametrize("malformed_result", ["missing-field", "wrong-shape"])
def test_completed_malformed_result_is_permanent_protocol_failure(
    tmp_path: Path,
    malformed_result: str,
) -> None:
    # Given
    controller = PendingController(malformed_result=malformed_result)
    runner, client = environment(controller)
    plan = plan_restore(
        RestoreRequest(inventory(installed=True), inventory(), None, ())
    )
    captures = iter((inventory(), inventory()))
    store = JournalStore(tmp_path / "env", "bot-1", MANIFEST_ID)
    restore_context = RestoreContext(
        store, runner, lambda: next(captures), RestoreOptions()
    )

    # When
    try:
        report = execute_restore(plan, restore_context)
    finally:
        runner.close()
        client.close()

    # Then
    assert report.exit_code == 1
    journal = store.load()
    assert journal is not None
    assert journal.operations[0].state is JournalState.FAILED
    assert journal.operations[0].attempts == 1
    assert journal.operations[0].error is not None
    assert "malformed" in journal.operations[0].error


def test_post_submit_process_interruption_recovers_same_attempt(tmp_path: Path) -> None:
    # Given
    controller = PendingController(interrupt=True)
    runner, client = environment(controller)
    plan = plan_restore(
        RestoreRequest(inventory(installed=True), inventory(), None, ())
    )
    restore_context, store = context(tmp_path, runner)

    # When
    try:
        with pytest.raises(ProcessInterrupted):
            _ = execute_restore(plan, restore_context)
        interrupted = store.load()
        report = execute_restore(plan, restore_context)
    finally:
        runner.close()
        client.close()

    # Then
    assert interrupted is not None
    assert interrupted.operations[0].state is JournalState.RUNNING
    assert interrupted.operations[0].attempts == 1
    assert len(set(controller.keys)) == 1
    assert controller.bodies[0] == controller.bodies[1]
    assert report.exit_code == 0
