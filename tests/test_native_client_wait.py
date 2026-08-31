from __future__ import annotations

import httpx
import pytest
from pydantic import JsonValue

from groken.native_client import NativeControllerClient
from groken.native_models import NativeOperationRequest, TerminalExec
from groken.native_wait_models import (
    NativeResultError,
    NativeTerminalFailureError,
    NativeWaitTimeoutError,
)


def _request() -> NativeOperationRequest:
    return NativeOperationRequest(
        target="box",
        workspace="restore",
        operation=TerminalExec(
            type="terminal.exec",
            argv=["/usr/bin/printf", "%s", "; literal"],
            stdin_b64="aW5wdXQ=",
            timeout_ms=30_000,
        ),
    )


def _terminal_record(
    request: NativeOperationRequest,
    *,
    status: str = "completed",
    result: dict[str, JsonValue] | None = None,
) -> dict[str, JsonValue]:
    return {
        "operation_id": "op-1",
        "status": status,
        "target": request.target,
        "workspace": request.workspace,
        "operation": request.operation.model_dump(mode="json"),
        "result": result,
        "error": "worker failed" if status == "failed" else None,
    }


def _success_result() -> dict[str, JsonValue]:
    return {
        "type": "terminal.exec",
        "exit_code": 0,
        "stdout_b64": "b3V0",
        "stderr_b64": "ZXJy",
        "timed_out": False,
        "truncated": False,
        "signal": None,
    }


def test_wait_calls_authenticated_event_endpoint_once() -> None:
    request = _request()
    calls: list[httpx.Request] = []

    def handler(http_request: httpx.Request) -> httpx.Response:
        calls.append(http_request)
        return httpx.Response(
            200, json=_terminal_record(request, result=_success_result())
        )

    transport = httpx.MockTransport(handler)
    with httpx.Client(
        transport=transport, base_url="http://controller.test"
    ) as http_client:
        client = NativeControllerClient(token="secret", client=http_client)

        record = client.wait("op-1", timeout_s=2.5)

    assert record.operation_id == "op-1"
    assert record.status == "completed"
    assert len(calls) == 1
    assert calls[0].url.path == "/v2/operations/op-1/wait"
    assert calls[0].url.params["timeout_s"] == "2.5"
    assert calls[0].headers["authorization"] == "Bearer secret"


def test_execute_wait_submits_once_and_preserves_exact_argv_and_result() -> None:
    request = _request()
    calls: list[httpx.Request] = []

    def handler(http_request: httpx.Request) -> httpx.Response:
        calls.append(http_request)
        if http_request.method == "POST":
            return httpx.Response(
                202, json={"operation_id": "op-1", "status": "queued"}
            )
        return httpx.Response(
            200, json=_terminal_record(request, result=_success_result())
        )

    transport = httpx.MockTransport(handler)
    with httpx.Client(
        transport=transport, base_url="http://controller.test"
    ) as http_client:
        client = NativeControllerClient(token="secret", client=http_client)

        result = client.execute_wait(
            request, idempotency_key="journal-key", timeout_s=3
        )

    assert [call.method for call in calls] == ["POST", "GET"]
    assert calls[0].headers["idempotency-key"] == "journal-key"
    assert calls[0].read().decode().count("; literal") == 1
    assert result.argv == ("/usr/bin/printf", "%s", "; literal")
    assert result.exit_code == 0
    assert result.signal is None
    assert result.stdout == b"out"
    assert result.stderr == b"err"
    assert result.timed_out is False
    assert result.truncated is False


def test_execute_wait_normalizes_process_signal() -> None:
    request = _request()
    signalled = {**_success_result(), "exit_code": -15}

    def handler(http_request: httpx.Request) -> httpx.Response:
        if http_request.method == "POST":
            return httpx.Response(
                202, json={"operation_id": "op-1", "status": "queued"}
            )
        return httpx.Response(200, json=_terminal_record(request, result=signalled))

    transport = httpx.MockTransport(handler)
    with httpx.Client(
        transport=transport, base_url="http://controller.test"
    ) as http_client:
        client = NativeControllerClient(token="secret", client=http_client)

        result = client.execute_wait(request, idempotency_key="signal-key", timeout_s=3)

    assert result.exit_code is None
    assert result.signal == 15


def test_execute_wait_returns_typed_terminal_failure() -> None:
    request = _request()
    terminal = _terminal_record(request, status="failed", result=None)

    def handler(http_request: httpx.Request) -> httpx.Response:
        if http_request.method == "POST":
            return httpx.Response(
                202, json={"operation_id": "op-1", "status": "queued"}
            )
        return httpx.Response(200, json=terminal)

    transport = httpx.MockTransport(handler)
    with httpx.Client(
        transport=transport, base_url="http://controller.test"
    ) as http_client:
        client = NativeControllerClient(token="secret", client=http_client)

        with pytest.raises(NativeTerminalFailureError, match="worker failed"):
            _ = client.execute_wait(request, idempotency_key="failed-key", timeout_s=3)


def test_execute_wait_rejects_malformed_terminal_result() -> None:
    request = _request()
    terminal = _terminal_record(
        request,
        result={**_success_result(), "stdout_b64": "%%%"},
    )

    def handler(http_request: httpx.Request) -> httpx.Response:
        if http_request.method == "POST":
            return httpx.Response(
                202, json={"operation_id": "op-1", "status": "queued"}
            )
        return httpx.Response(200, json=terminal)

    transport = httpx.MockTransport(handler)
    with httpx.Client(
        transport=transport, base_url="http://controller.test"
    ) as http_client:
        client = NativeControllerClient(token="secret", client=http_client)

        with pytest.raises(NativeResultError, match="malformed"):
            _ = client.execute_wait(
                request, idempotency_key="malformed-key", timeout_s=3
            )


def test_execute_wait_timeout_and_ambiguous_disconnect_do_not_resubmit() -> None:
    request = _request()
    post_count = 0

    def handler(http_request: httpx.Request) -> httpx.Response:
        nonlocal post_count
        if http_request.method == "POST":
            post_count += 1
            return httpx.Response(
                202, json={"operation_id": "op-1", "status": "queued"}
            )
        raise httpx.ReadTimeout("connection interrupted", request=http_request)

    transport = httpx.MockTransport(handler)
    with httpx.Client(
        transport=transport, base_url="http://controller.test"
    ) as http_client:
        client = NativeControllerClient(token="secret", client=http_client)

        with pytest.raises(httpx.ReadTimeout):
            _ = client.execute_wait(
                request, idempotency_key="recovery-key", timeout_s=3
            )
        assert post_count == 1
        with pytest.raises(httpx.ReadTimeout):
            _ = client.execute_wait(
                request, idempotency_key="recovery-key", timeout_s=3
            )

    assert post_count == 2


@pytest.mark.parametrize("response_kind", ["timeout", "nonterminal"])
def test_wait_rejects_typed_timeout_and_nonterminal_response(
    response_kind: str,
) -> None:
    request = _request()
    response = (
        httpx.Response(
            408,
            json={"operation_id": "op-1", "status": "timeout", "timeout_s": 1.0},
        )
        if response_kind == "timeout"
        else httpx.Response(
            200,
            json=_terminal_record(request, status="queued", result=None),
        )
    )
    transport = httpx.MockTransport(lambda _request: response)
    with httpx.Client(
        transport=transport, base_url="http://controller.test"
    ) as http_client:
        client = NativeControllerClient(token="secret", client=http_client)

        expected_error = (
            NativeWaitTimeoutError if response_kind == "timeout" else ValueError
        )
        with pytest.raises(expected_error):
            _ = client.wait("op-1", timeout_s=1)


def test_execute_wait_stops_on_idempotency_conflict() -> None:
    methods: list[str] = []

    def handler(http_request: httpx.Request) -> httpx.Response:
        methods.append(http_request.method)
        return httpx.Response(409, json={"detail": "idempotency conflict"})

    transport = httpx.MockTransport(handler)
    with httpx.Client(
        transport=transport, base_url="http://controller.test"
    ) as http_client:
        client = NativeControllerClient(token="secret", client=http_client)

        with pytest.raises(httpx.HTTPStatusError):
            _ = client.execute_wait(
                _request(), idempotency_key="conflict-key", timeout_s=1
            )

    assert methods == ["POST"]
