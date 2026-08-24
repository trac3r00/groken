import json
import stat
from pathlib import Path
from typing import Protocol, cast

from fastapi.testclient import TestClient

from groken.worker_app import WorkerSettings, create_app
from groken.worker_models import JobExecution, JobRequest


class Runner(Protocol):
    async def run(self, job_id: str, request: JobRequest, workspace: Path) -> JobExecution: ...


class FakeRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Path]] = []

    async def run(self, job_id: str, request: JobRequest, workspace: Path) -> JobExecution:
        self.calls.append((job_id, request.task, workspace))
        return JobExecution(result="REMOTE_AGENT_OK", exit_code=0)


class FakeCallbackSender:
    def __init__(self) -> None:
        self.payloads: list[dict[str, object]] = []

    async def send(self, request: JobRequest, payload: dict[str, object]) -> None:
        _ = request
        self.payloads.append(payload)


def make_client(tmp_path: Path) -> tuple[TestClient, FakeRunner, FakeCallbackSender, WorkerSettings]:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    settings = WorkerSettings(
        state_dir=tmp_path / "state",
        workspace_root=workspace_root,
        bootstrap_token="bootstrap-token-with-at-least-32-chars",
        omo_command="/usr/local/bin/omo",
    )
    runner = FakeRunner()
    callbacks = FakeCallbackSender()
    app = create_app(settings, runner=runner, callback_sender=callbacks)
    return TestClient(app), runner, callbacks, settings


def bootstrap(client: TestClient) -> str:
    worker_token = "worker-token-with-at-least-thirty-two-characters"
    response = client.post(
        "/v1/bootstrap",
        headers={"x-bootstrap-token": "bootstrap-token-with-at-least-32-chars"},
        json={
            "model_base_url": "https://models.example.test/v1",
            "model_api_key": "model-secret",
            "worker_token": worker_token,
            "model": "llm-pool/codex/gpt-5.6-luna",
        },
    )
    assert response.status_code == 201
    return worker_token


def test_bootstrap_is_authenticated_and_one_time(tmp_path: Path) -> None:
    client, _, _, settings = make_client(tmp_path)

    denied = client.post("/v1/bootstrap", headers={"x-bootstrap-token": "wrong"}, json={})
    assert denied.status_code == 401

    worker_token = bootstrap(client)
    assert worker_token
    assert stat.S_IMODE(settings.secrets_file.stat().st_mode) == 0o600
    stored = cast("dict[str, object]", json.loads(settings.secrets_file.read_text()))
    assert stored["model_api_key"] == "model-secret"

    repeated = client.post(
        "/v1/bootstrap",
        headers={"x-bootstrap-token": "bootstrap-token-with-at-least-32-chars"},
        json={
            "model_base_url": "https://models.example.test/v1",
            "model_api_key": "other-secret",
            "worker_token": worker_token,
            "model": "llm-pool/codex/gpt-5.6-luna",
        },
    )
    assert repeated.status_code == 409


def test_job_runs_and_callback_contains_correlation(tmp_path: Path) -> None:
    client, runner, callbacks, _ = make_client(tmp_path)
    worker_token = bootstrap(client)

    unauthorized = client.post("/v1/jobs", json={"task": "test", "workspace": "qa"})
    assert unauthorized.status_code == 401

    accepted = client.post(
        "/v1/jobs",
        headers={"authorization": f"Bearer {worker_token}"},
        json={
            "task": "write proof.txt",
            "workspace": "qa/checkout",
            "origin_session_id": "origin-session-1",
            "callback_url": "https://controller.example.test/results",
            "callback_token": "callback-secret-with-at-least-32-characters",
        },
    )
    assert accepted.status_code == 202
    accepted_body = cast("dict[str, object]", accepted.json())
    job_id = cast("str", accepted_body["job_id"])

    status_response = client.get(
        f"/v1/jobs/{job_id}", headers={"authorization": f"Bearer {worker_token}"}
    )
    assert status_response.status_code == 200
    assert status_response.json()["status"] == "completed"
    assert status_response.json()["result"] == "REMOTE_AGENT_OK"
    assert runner.calls == [(job_id, "write proof.txt", tmp_path / "workspace" / "qa" / "checkout")]
    assert callbacks.payloads[0]["job_id"] == job_id
    assert callbacks.payloads[0]["origin_session_id"] == "origin-session-1"
    assert callbacks.payloads[0]["status"] == "completed"
