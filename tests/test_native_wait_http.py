from __future__ import annotations

import threading
import time
from pathlib import Path

from fastapi.testclient import TestClient

from groken.controller_app import ControllerSettings, create_controller_app
from groken.native_models import (
    NativeAccepted,
    NativeLeasedOperation,
    NativeOperationRecord,
)

_CONTROLLER_HEADERS = {"authorization": "Bearer controller-token"}
_WORKER_HEADERS = {"authorization": "Bearer worker-token"}


class _FakeNativeEmitter:
    def __init__(self) -> None:
        self.records: list[NativeOperationRecord] = []

    async def emit(self, record: NativeOperationRecord) -> None:
        self.records.append(record)


def _app(tmp_path: Path):
    return create_controller_app(
        ControllerSettings(
            state_dir=tmp_path / "controller",
            controller_token="controller-token",
            enrollment_token="enrollment-token",
            worker_token="worker-token",
            model_base_url="https://models.example.test/v1",
            model_api_key="model-secret",
            model="test-model",
        ),
        native_emitter=_FakeNativeEmitter(),
    )


def _request(target: str = "box") -> dict[str, str | dict[str, str | list[str]]]:
    return {
        "target": target,
        "workspace": "restore",
        "operation": {
            "type": "terminal.exec",
            "argv": ["/usr/bin/printf", "%s", "; literal"],
        },
    }


def _submit(client: TestClient, key: str, target: str = "box") -> NativeAccepted:
    response = client.post(
        "/v2/operations",
        headers={**_CONTROLLER_HEADERS, "idempotency-key": key},
        json=_request(target),
    )
    assert response.status_code == 202
    return NativeAccepted.model_validate(response.json())


def _lease(client: TestClient, worker_id: str = "box") -> NativeLeasedOperation:
    response = client.post(
        "/v2/worker/lease",
        headers=_WORKER_HEADERS,
        json={"worker_id": worker_id, "capabilities": ["terminal"]},
    )
    assert response.status_code == 200
    return NativeLeasedOperation.model_validate(response.json())


def _complete(
    client: TestClient,
    lease: NativeLeasedOperation,
    *,
    worker_id: str = "box",
    status: str = "completed",
    exit_code: int = 0,
) -> None:
    response = client.post(
        "/v2/worker/complete",
        headers=_WORKER_HEADERS,
        json={
            "worker_id": worker_id,
            "operation_id": lease.operation_id,
            "lease_id": lease.lease_id,
            "status": status,
            "result": {
                "type": "terminal.exec",
                "exit_code": exit_code,
                "stdout_b64": "b2s=",
                "stderr_b64": "",
                "timed_out": False,
                "truncated": False,
                "signal": None,
            },
            "error": "worker failed" if status == "failed" else None,
        },
    )
    assert response.status_code == 202


def test_waiter_established_then_completion_wakes_with_exact_terminal_json(
    tmp_path: Path,
) -> None:
    with TestClient(_app(tmp_path)) as client:
        accepted = _submit(client, "wake-key")
        lease = _lease(client)
        request_started = threading.Event()
        request_finished = threading.Event()
        completion_allowed = threading.Event()
        responses = []

        def wait_request() -> None:
            request_started.set()
            responses.append(
                client.get(
                    f"/v2/operations/{accepted.operation_id}/wait",
                    headers=_CONTROLLER_HEADERS,
                    params={"timeout_s": 2},
                )
            )
            request_finished.set()

        def complete_request() -> None:
            assert completion_allowed.wait(timeout=2)
            _complete(client, lease)

        waiter = threading.Thread(target=wait_request)
        completer = threading.Thread(target=complete_request)
        waiter.start()
        completer.start()
        assert request_started.wait(timeout=2)
        assert not request_finished.is_set()
        completion_allowed.set()
        assert request_finished.wait(timeout=2)
        waiter.join(timeout=2)
        completer.join(timeout=2)
        assert not waiter.is_alive()
        assert not completer.is_alive()

        response = responses[0]
        assert response.status_code == 200
        body = response.json()
        assert body["operation_id"] == accepted.operation_id
        assert body["status"] == "completed"
        assert body["operation"]["argv"] == ["/usr/bin/printf", "%s", "; literal"]
        assert body["result"] == {
            "type": "terminal.exec",
            "exit_code": 0,
            "stdout_b64": "b2s=",
            "stderr_b64": "",
            "timed_out": False,
            "truncated": False,
            "signal": None,
        }


def test_completion_before_wait_returns_immediately(tmp_path: Path) -> None:
    with TestClient(_app(tmp_path)) as client:
        accepted = _submit(client, "already-complete")
        lease = _lease(client)
        _complete(client, lease)

        response = client.get(
            f"/v2/operations/{accepted.operation_id}/wait",
            headers=_CONTROLLER_HEADERS,
            params={"timeout_s": 1},
        )

        assert response.status_code == 200
        assert response.json()["status"] == "completed"


def test_wait_timeout_is_bounded_and_typed(tmp_path: Path) -> None:
    with TestClient(_app(tmp_path)) as client:
        accepted = _submit(client, "timeout")

        started = time.monotonic()
        response = client.get(
            f"/v2/operations/{accepted.operation_id}/wait",
            headers=_CONTROLLER_HEADERS,
            params={"timeout_s": 0.01},
        )
        elapsed = time.monotonic() - started

        assert response.status_code == 408
        assert response.json() == {
            "operation_id": accepted.operation_id,
            "status": "timeout",
            "timeout_s": 0.01,
        }
        assert elapsed < 1

        lease = _lease(client)
        _complete(client, lease)
        recovered = client.get(
            f"/v2/operations/{accepted.operation_id}/wait",
            headers=_CONTROLLER_HEADERS,
            params={"timeout_s": 1},
        )
        assert recovered.status_code == 200
        assert recovered.json()["operation_id"] == accepted.operation_id


def test_concurrent_waiters_for_same_operation_wake_once(tmp_path: Path) -> None:
    with TestClient(_app(tmp_path)) as client:
        accepted = _submit(client, "concurrent")
        lease = _lease(client)
        started = [threading.Event(), threading.Event()]
        finished = [threading.Event(), threading.Event()]
        responses = []

        def wait_request(index: int) -> None:
            started[index].set()
            responses.append(
                client.get(
                    f"/v2/operations/{accepted.operation_id}/wait",
                    headers=_CONTROLLER_HEADERS,
                    params={"timeout_s": 2},
                )
            )
            finished[index].set()

        waiters = [
            threading.Thread(target=wait_request, args=(index,)) for index in range(2)
        ]
        for waiter in waiters:
            waiter.start()
        assert all(event.wait(timeout=2) for event in started)
        _complete(client, lease)
        assert all(event.wait(timeout=2) for event in finished)
        for waiter in waiters:
            waiter.join(timeout=2)
            assert not waiter.is_alive()

        assert len(responses) == 2
        assert all(response.status_code == 200 for response in responses)
        assert {response.json()["operation_id"] for response in responses} == {
            accepted.operation_id
        }


def test_wrong_operation_completion_does_not_complete_waiter(tmp_path: Path) -> None:
    with TestClient(_app(tmp_path)) as client:
        first = _submit(client, "first", "box")
        first_lease = _lease(client, "box")
        _ = _submit(client, "second", "other-box")
        second_lease = _lease(client, "other-box")
        finished = threading.Event()
        responses = []

        def wait_request() -> None:
            responses.append(
                client.get(
                    f"/v2/operations/{first.operation_id}/wait",
                    headers=_CONTROLLER_HEADERS,
                    params={"timeout_s": 2},
                )
            )
            finished.set()

        waiter = threading.Thread(target=wait_request)
        waiter.start()
        _complete(client, second_lease, worker_id="other-box")
        assert not finished.is_set()
        _complete(client, first_lease)
        assert finished.wait(timeout=2)
        waiter.join(timeout=2)

        assert responses[0].json()["operation_id"] == first.operation_id


def test_wait_auth_and_idempotency_contract(tmp_path: Path) -> None:
    with TestClient(_app(tmp_path)) as client:
        first = _submit(client, "stable-key")
        retry = _submit(client, "stable-key")
        conflict = client.post(
            "/v2/operations",
            headers={**_CONTROLLER_HEADERS, "idempotency-key": "stable-key"},
            json=_request("different-box"),
        )
        denied = client.get(
            f"/v2/operations/{first.operation_id}/wait",
            params={"timeout_s": 0.01},
        )

        assert retry.operation_id == first.operation_id
        assert conflict.status_code == 409
        assert denied.status_code == 401
