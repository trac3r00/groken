from __future__ import annotations

import httpx
import pytest

from groken.env_collectors import (
    CommandRequest,
    CommandResult,
    NativePlaneUnavailable,
    collect_environment,
)
from groken.env_native_runner import NativeAdapterError, NativeEnvironmentRunner
from groken.env_persistence import ManifestTree, TreeFile
from groken.native_client import NativeControllerClient
from groken.native_models import NativeOperationRequest


def native_runner(handler: httpx.MockTransport) -> tuple[NativeEnvironmentRunner, httpx.Client]:
    http_client = httpx.Client(transport=handler, base_url="http://controller.test")
    client = NativeControllerClient(token="test-token", client=http_client)
    return NativeEnvironmentRunner(client), http_client


@pytest.mark.parametrize("status_code", [None, 401, 403, 404, 408, 429, 502, 503, 504])
def test_native_capture_connection_failure_is_plane_unavailability(
    status_code: int | None,
) -> None:
    # Given
    def disconnect(request: httpx.Request) -> httpx.Response:
        if status_code is None:
            raise httpx.ConnectError("controller unavailable", request=request)
        return httpx.Response(status_code)

    runner, client = native_runner(httpx.MockTransport(disconnect))

    # When / Then
    try:
        with pytest.raises(NativePlaneUnavailable, match="controller unavailable"):
            _ = runner.task4_runner("bot-1").run(
                CommandRequest(("/usr/bin/uname", "-s"))
            )
    finally:
        runner.close()
        client.close()


@pytest.mark.parametrize("terminal_failure", [False, True])
def test_native_capture_terminal_unavailability_uses_chat_fallback_signal(
    terminal_failure: bool,
) -> None:
    # Given
    requests: dict[str, NativeOperationRequest] = {}

    def unavailable(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            requests["op-1"] = NativeOperationRequest.model_validate_json(
                request.content
            )
            return httpx.Response(202, json={"operation_id": "op-1", "status": "queued"})
        if not terminal_failure:
            return httpx.Response(
                408,
                json={"operation_id": "op-1", "status": "timeout", "timeout_s": 35.0},
            )
        operation = requests["op-1"]
        return httpx.Response(
            200,
            json={
                "operation_id": "op-1",
                "status": "failed",
                "target": operation.target,
                "workspace": operation.workspace,
                "operation": operation.operation.model_dump(mode="json"),
                "result": None,
                "error": "worker unavailable",
            },
        )

    runner, client = native_runner(httpx.MockTransport(unavailable))

    # When / Then
    try:
        with pytest.raises(NativePlaneUnavailable):
            _ = runner.task4_runner("bot-1").run(
                CommandRequest(("/usr/bin/uname", "-s"))
            )
    finally:
        runner.close()
        client.close()


def test_native_capture_idempotency_conflict_stays_loud() -> None:
    # Given
    def conflict(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409)

    runner, client = native_runner(httpx.MockTransport(conflict))

    # When / Then
    try:
        with pytest.raises(NativeAdapterError, match="409 Conflict"):
            _ = runner.task4_runner("bot-1").run(
                CommandRequest(("/usr/bin/uname", "-s"))
            )
    finally:
        runner.close()
        client.close()


def test_native_capture_mismatched_result_stays_loud() -> None:
    # Given
    requests: dict[str, NativeOperationRequest] = {}

    def mismatch(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            requests["op-1"] = NativeOperationRequest.model_validate_json(
                request.content
            )
            return httpx.Response(202, json={"operation_id": "op-1", "status": "queued"})
        operation = requests["op-1"]
        return httpx.Response(
            200,
            json={
                "operation_id": "other-operation",
                "status": "failed",
                "target": operation.target,
                "workspace": operation.workspace,
                "operation": operation.operation.model_dump(mode="json"),
                "result": None,
                "error": "worker failed",
            },
        )

    runner, client = native_runner(httpx.MockTransport(mismatch))

    # When / Then
    try:
        with pytest.raises(NativeAdapterError, match="different operation"):
            _ = runner.task4_runner("bot-1").run(
                CommandRequest(("/usr/bin/uname", "-s"))
            )
    finally:
        runner.close()
        client.close()


def test_native_capture_malformed_controller_data_stays_loud() -> None:
    # Given
    def malformed(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(202, json={"operation_id": "op-1", "status": "queued"})
        return httpx.Response(200, json={"operation_id": "op-1", "status": "completed"})

    runner, client = native_runner(httpx.MockTransport(malformed))

    # When / Then
    try:
        with pytest.raises(NativeAdapterError):
            _ = runner.task4_runner("bot-1").run(
                CommandRequest(("/usr/bin/uname", "-s"))
            )
    finally:
        runner.close()
        client.close()


def test_native_capture_malformed_base64_stays_loud() -> None:
    # Given
    requests: dict[str, NativeOperationRequest] = {}

    def malformed_base64(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            requests["op-1"] = NativeOperationRequest.model_validate_json(
                request.content
            )
            return httpx.Response(202, json={"operation_id": "op-1", "status": "queued"})
        operation = requests["op-1"]
        return httpx.Response(
            200,
            json={
                "operation_id": "op-1",
                "status": "completed",
                "target": operation.target,
                "workspace": operation.workspace,
                "operation": operation.operation.model_dump(mode="json"),
                "result": {
                    "type": "terminal.exec",
                    "exit_code": 0,
                    "stdout_b64": "not-base64!",
                    "stderr_b64": "",
                    "timed_out": False,
                    "truncated": False,
                    "signal": None,
                },
                "error": None,
            },
        )

    runner, client = native_runner(httpx.MockTransport(malformed_base64))

    # When / Then
    try:
        with pytest.raises(NativeAdapterError, match="malformed terminal.exec result"):
            _ = runner.task4_runner("bot-1").run(
                CommandRequest(("/usr/bin/uname", "-s"))
            )
    finally:
        runner.close()
        client.close()


def test_native_adapter_error_escapes_collector_isolation() -> None:
    # Given
    class FailingCollectorRunner:
        def __init__(self) -> None:
            self.calls = 0

        def run(self, request: CommandRequest) -> CommandResult:
            self.calls += 1
            if self.calls == 4:
                raise NativeAdapterError("collector adapter failed")
            return CommandResult(request.argv, 0, b"", b"", False)

    # When / Then
    with pytest.raises(NativeAdapterError, match="collector adapter failed"):
        _ = collect_environment(FailingCollectorRunner())


def test_native_manifest_path_adapter_bug_escapes_collector_isolation() -> None:
    # Given
    def unexpected(request: httpx.Request) -> httpx.Response:
        raise AssertionError(request)

    runner, client = native_runner(httpx.MockTransport(unexpected))

    # When / Then
    try:
        with pytest.raises(NativeAdapterError) as captured:
            runner.task4_runner("bot-1").publish(
                ManifestTree("sha256:fixture", (TreeFile("../escape", b"bad"),))
            )
    finally:
        runner.close()
        client.close()
    assert not isinstance(captured.value, NativePlaneUnavailable)
