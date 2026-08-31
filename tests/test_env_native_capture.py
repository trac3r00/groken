from __future__ import annotations

import base64
import uuid
from typing import final

import httpx
import pytest

from groken.env_collectors import CommandRequest
from groken.env_native_runner import CapturePhase, NativeEnvironmentRunner
from groken.env_persistence import ManifestTree, TreeFile
from groken.native_client import NativeControllerClient
from groken.native_models import NativeOperationRequest, TerminalExec

MANIFEST_ID = "sha256:" + "7" * 64


@final
class CaptureController:
    def __init__(self) -> None:
        self.requests: dict[str, NativeOperationRequest] = {}
        self.keys: list[str] = []
        self.truncated = False

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
        key = request.url.path.split("/")[-2]
        operation = self.requests[key]
        terminal = TerminalExec.model_validate(operation.operation.model_dump())
        unavailable = terminal.argv[0] == "/usr/bin/which"
        return httpx.Response(
            200,
            json={
                "operation_id": key,
                "status": "completed",
                "target": operation.target,
                "workspace": operation.workspace,
                "operation": operation.operation.model_dump(mode="json"),
                "result": {
                    "type": "terminal.exec",
                    "exit_code": 1 if unavailable else 0,
                    "stdout_b64": base64.b64encode(b"" if unavailable else b"out").decode(),
                    "stderr_b64": "",
                    "timed_out": False,
                    "truncated": self.truncated,
                    "signal": None,
                },
                "error": None,
            },
        )


def environment(
    controller: CaptureController,
) -> tuple[NativeEnvironmentRunner, httpx.Client]:
    http_client = httpx.Client(
        transport=httpx.MockTransport(controller.handle),
        base_url="http://controller.test",
    )
    native = NativeControllerClient(token="test-token", client=http_client)
    return NativeEnvironmentRunner(native), http_client


def test_task4_publish_uses_fresh_scope_and_exact_argv_and_stdin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scopes = iter((uuid.UUID(int=1), uuid.UUID(int=2)))
    monkeypatch.setattr("groken.env_native_runner.uuid.uuid4", lambda: next(scopes))
    controller = CaptureController()
    runner, client = environment(controller)
    tree = ManifestTree(MANIFEST_ID, (TreeFile("artifacts/brew.raw", b'brew "jq"\n'),))
    try:
        runner.task4_runner("bot-1").publish(tree)
        midpoint = len(controller.keys)
        runner.task4_runner("bot-1").publish(tree)
    finally:
        runner.close()
        client.close()
    assert midpoint == 3
    assert controller.keys[:midpoint] != controller.keys[midpoint:]
    assert len(controller.requests) == midpoint * 2
    operations = [
        TerminalExec.model_validate(
            controller.requests[key].operation.model_dump(),
        )
        for key in controller.keys[:midpoint]
    ]
    destination = f"groken-env/manifests/{MANIFEST_ID}/artifacts/brew.raw"
    assert [tuple(operation.argv) for operation in operations] == [
        ("/usr/bin/env", "mkdir", "-m", "700", "-p", destination.rsplit("/", 1)[0]),
        ("/usr/bin/env", "tee", destination),
        ("/usr/bin/env", "chmod", "600", destination),
    ]
    assert operations[1].stdin_b64 == base64.b64encode(b'brew "jq"\n').decode()


def test_native_recapture_uses_fresh_phase_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scopes = iter((uuid.UUID(int=1), uuid.UUID(int=2)))
    monkeypatch.setattr("groken.env_native_runner.uuid.uuid4", lambda: next(scopes))
    controller = CaptureController()
    runner, client = environment(controller)
    try:
        first = runner.capture(MANIFEST_ID, CapturePhase.POST_RESTORE)
        midpoint = len(controller.keys)
        second = runner.capture(MANIFEST_ID, CapturePhase.POST_RESTORE)
    finally:
        runner.close()
        client.close()
    assert first == second
    assert midpoint > 0
    assert controller.keys[:midpoint] != controller.keys[midpoint:]
    assert len(controller.requests) == midpoint * 2


def test_native_capture_propagates_truncated_result() -> None:
    # Given
    controller = CaptureController()
    controller.truncated = True
    runner, client = environment(controller)

    # When
    try:
        result = runner.task4_runner("bot-1").run(
            CommandRequest(("/usr/bin/uname", "-s"))
        )
    finally:
        runner.close()
        client.close()

    # Then
    assert result.exit_code == 0
    assert result.truncated is True
