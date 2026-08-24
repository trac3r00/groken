from pathlib import Path
from typing import cast

from fastapi.testclient import TestClient

from groken.controller_app import ControllerSettings, create_controller_app
from groken.native_models import NativeOperationRecord, NativeStatus


class FakeNativeEmitter:
    def __init__(self) -> None:
        self.records: list[NativeOperationRecord] = []

    async def emit(self, record: NativeOperationRecord) -> None:
        self.records.append(record)


def make_client(tmp_path: Path) -> tuple[TestClient, FakeNativeEmitter]:
    settings = ControllerSettings(
        state_dir=tmp_path / "controller",
        controller_token="controller-token-with-at-least-32-characters",
        enrollment_token="enrollment-token-with-at-least-32-characters",
        worker_token="worker-token-with-at-least-32-characters",
        model_base_url="https://models.example.test/v1",
        model_api_key="model-secret",
        model="llm-pool/codex/gpt-5.6-luna",
    )
    emitter = FakeNativeEmitter()
    app = create_controller_app(settings, native_emitter=emitter)
    return TestClient(app), emitter


def test_native_submit_lease_complete_without_prompt(tmp_path: Path) -> None:
    client, emitter = make_client(tmp_path)
    controller_headers = {
        "authorization": "Bearer controller-token-with-at-least-32-characters",
        "idempotency-key": "native-request-1",
    }
    worker_headers = {"authorization": "Bearer worker-token-with-at-least-32-characters"}
    request = {
        "target": "groken-box",
        "workspace": "native-proof",
        "origin_session_id": "origin-session",
        "operation": {
            "type": "terminal.exec",
            "argv": ["/usr/bin/printf", "native"],
        },
    }

    submitted = client.post("/v2/operations", headers=controller_headers, json=request)
    retried = client.post("/v2/operations", headers=controller_headers, json=request)
    assert submitted.status_code == 202
    assert submitted.json()["operation_id"] == retried.json()["operation_id"]
    operation_id = cast("str", submitted.json()["operation_id"])

    leased = client.post(
        "/v2/worker/lease",
        headers=worker_headers,
        json={"worker_id": "groken-box", "capabilities": ["terminal", "files"]},
    )
    assert leased.status_code == 200
    assert leased.json()["operation_id"] == operation_id
    assert "task" not in leased.json()

    completed = client.post(
        "/v2/worker/complete",
        headers=worker_headers,
        json={
            "worker_id": "groken-box",
            "operation_id": operation_id,
            "lease_id": leased.json()["lease_id"],
            "status": "completed",
            "result": {"type": "terminal.exec", "exit_code": 0},
            "error": None,
        },
    )
    assert completed.status_code == 202

    status_response = client.get(
        f"/v2/operations/{operation_id}", headers=controller_headers
    )
    record = NativeOperationRecord.model_validate(status_response.json())
    assert record.status is NativeStatus.COMPLETED
    assert record.result == {"type": "terminal.exec", "exit_code": 0}
    assert len(emitter.records) == 1
