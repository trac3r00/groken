from pathlib import Path
from typing import cast

from fastapi.testclient import TestClient

from groken.controller_app import ControllerSettings, create_controller_app
from groken.controller_models import ControllerJobRecord, ControllerJobStatus


class FakeEmitter:
    def __init__(self) -> None:
        self.records: list[ControllerJobRecord] = []

    async def emit(self, record: ControllerJobRecord) -> None:
        self.records.append(record)


def make_client(tmp_path: Path) -> tuple[TestClient, FakeEmitter]:
    settings = ControllerSettings(
        state_dir=tmp_path / "controller",
        controller_token="controller-token-with-at-least-32-characters",
        enrollment_token="enrollment-token-with-at-least-32-characters",
        worker_token="worker-token-with-at-least-32-characters",
        model_base_url="https://models.example.test/v1",
        model_api_key="model-secret",
        model="llm-pool/codex/gpt-5.6-luna",
    )
    emitter = FakeEmitter()
    app = create_controller_app(settings, emitter=emitter)
    return TestClient(app), emitter


def test_enrollment_is_authenticated(tmp_path: Path) -> None:
    client, _ = make_client(tmp_path)

    denied = client.post("/v1/enroll", headers={"x-enrollment-token": "wrong"})
    assert denied.status_code == 401

    enrolled = client.post(
        "/v1/enroll",
        headers={"x-enrollment-token": "enrollment-token-with-at-least-32-characters"},
        json={"worker_id": "groken-box"},
    )
    assert enrolled.status_code == 200
    body = cast("dict[str, object]", enrolled.json())
    assert body["worker_token"] == "worker-token-with-at-least-32-characters"
    assert body["model_api_key"] == "model-secret"
    assert body["model_base_url"] == "https://models.example.test/v1"


def test_submit_lease_complete_roundtrip(tmp_path: Path) -> None:
    client, emitter = make_client(tmp_path)
    controller_headers = {"authorization": "Bearer controller-token-with-at-least-32-characters"}
    worker_headers = {"authorization": "Bearer worker-token-with-at-least-32-characters"}

    denied = client.post("/v1/jobs", json={"task": "test", "workspace": "qa"})
    assert denied.status_code == 401

    submitted = client.post(
        "/v1/jobs",
        headers=controller_headers,
        json={
            "task": "create proof.txt",
            "workspace": "direct-worker-proof",
            "origin_session_id": "origin-session-1",
        },
    )
    assert submitted.status_code == 202
    submitted_body = cast("dict[str, object]", submitted.json())
    job_id = cast("str", submitted_body["job_id"])

    leased = client.post(
        "/v1/worker/lease",
        headers=worker_headers,
        json={"worker_id": "groken-box"},
    )
    assert leased.status_code == 200
    assert leased.json()["job_id"] == job_id
    assert leased.json()["task"] == "create proof.txt"
    lease_id = cast("str", leased.json()["lease_id"])

    recovered = client.post(
        "/v1/worker/lease",
        headers=worker_headers,
        json={"worker_id": "groken-box"},
    )
    assert recovered.status_code == 200
    assert recovered.json()["lease_id"] == lease_id

    completed = client.post(
        "/v1/worker/complete",
        headers=worker_headers,
        json={
            "worker_id": "groken-box",
            "job_id": job_id,
            "lease_id": lease_id,
            "status": "completed",
            "result": "REMOTE_DIRECT_OK",
            "error": None,
        },
    )
    assert completed.status_code == 202

    status_response = client.get(f"/v1/jobs/{job_id}", headers=controller_headers)
    record = ControllerJobRecord.model_validate(status_response.json())
    assert record.status is ControllerJobStatus.COMPLETED
    assert record.result == "REMOTE_DIRECT_OK"
    assert emitter.records[0].job_id == job_id

    retried = client.post(
        "/v1/worker/complete",
        headers=worker_headers,
        json={
            "worker_id": "groken-box",
            "job_id": job_id,
            "lease_id": lease_id,
            "status": "completed",
            "result": "REMOTE_DIRECT_OK",
            "error": None,
        },
    )
    assert retried.status_code == 202
    assert len(emitter.records) == 1


def test_submit_idempotency_key_reuses_job_and_rejects_conflict(tmp_path: Path) -> None:
    client, _ = make_client(tmp_path)
    headers = {
        "authorization": "Bearer controller-token-with-at-least-32-characters",
        "idempotency-key": "origin-session:request-1",
    }
    body = {"task": "verify", "workspace": "qa"}

    first = client.post("/v1/jobs", headers=headers, json=body)
    retried = client.post("/v1/jobs", headers=headers, json=body)
    conflict = client.post(
        "/v1/jobs",
        headers=headers,
        json={"task": "different", "workspace": "qa"},
    )

    assert first.status_code == 202
    assert retried.status_code == 202
    assert first.json()["job_id"] == retried.json()["job_id"]
    assert conflict.status_code == 409


def test_submit_rejects_workspace_escape(tmp_path: Path) -> None:
    client, _ = make_client(tmp_path)
    response = client.post(
        "/v1/jobs",
        headers={"authorization": "Bearer controller-token-with-at-least-32-characters"},
        json={"task": "escape", "workspace": "../outside"},
    )
    assert response.status_code == 422
