import json
from pathlib import Path
from typing import cast

import httpx
import pytest

from groken.controller_models import CompletionRequest, ControllerJobStatus
from groken.poller import PollerSettings, RemotePoller
from groken.worker_models import JobExecution, JobRequest


class FakeRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Path]] = []

    async def run(
        self, job_id: str, request: JobRequest, workspace: Path
    ) -> JobExecution:
        self.calls.append((job_id, request.task, workspace))
        return JobExecution(result="REMOTE_POLLER_OK", exit_code=0)


@pytest.mark.anyio
async def test_poller_enrolls_leases_executes_and_completes(tmp_path: Path) -> None:
    completed_payloads: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/enroll":
            assert request.headers["x-enrollment-token"] == "enrollment-secret"
            return httpx.Response(
                200,
                json={
                    "worker_token": "worker-token-with-at-least-32-characters",
                    "model_base_url": "https://models.example.test/v1",
                    "model_api_key": "model-secret",
                    "model": "llm-pool/codex/gpt-5.6-luna",
                },
            )
        if request.url.path == "/v1/worker/lease":
            assert (
                request.headers["authorization"]
                == "Bearer worker-token-with-at-least-32-characters"
            )
            return httpx.Response(
                200,
                json={
                    "job_id": "job-123",
                    "lease_id": "lease-123",
                    "lease_expires_at": "2026-08-21T06:00:00Z",
                    "task": "write proof.txt",
                    "workspace": "direct/proof",
                    "origin_session_id": "origin-session",
                },
            )
        if request.url.path == "/v1/worker/complete":
            completed_payloads.append(
                cast("dict[str, object]", json.loads(request.content))
            )
            return httpx.Response(202, json={"accepted": True})
        raise AssertionError(f"unexpected path: {request.url.path}")

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://controller.test"
    )
    runner = FakeRunner()
    settings = PollerSettings(
        controller_url="https://controller.test",
        enrollment_token="enrollment-secret",
        worker_id="groken-box",
        state_dir=tmp_path / "poller-state",
        workspace_root=tmp_path / "workspace",
        omo_command="/home/box/.local/bin/omo",
    )
    poller = RemotePoller(settings, runner=runner, client=client)

    worked = await poller.run_once()
    await client.aclose()

    assert worked is True
    assert runner.calls == [
        ("job-123", "write proof.txt", tmp_path / "workspace" / "direct" / "proof")
    ]
    assert completed_payloads == [
        {
            "worker_id": "groken-box",
            "job_id": "job-123",
            "lease_id": "lease-123",
            "status": "completed",
            "result": "REMOTE_POLLER_OK",
            "error": None,
        }
    ]
    secrets = cast(
        "dict[str, object]",
        json.loads((settings.state_dir / "secrets.json").read_text()),
    )
    assert secrets["model_api_key"] == "model-secret"


@pytest.mark.anyio
async def test_poller_persists_completion_until_acknowledged(tmp_path: Path) -> None:
    calls = {"lease": 0, "complete": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/enroll":
            return httpx.Response(
                200,
                json={
                    "worker_token": "worker-token-with-at-least-32-characters",
                    "model_base_url": "https://models.example.test/v1",
                    "model_api_key": "model-secret",
                    "model": "llm-pool/codex/gpt-5.6-luna",
                },
            )
        if request.url.path == "/v1/worker/lease":
            calls["lease"] += 1
            if calls["lease"] > 1:
                return httpx.Response(204)
            return httpx.Response(
                200,
                json={
                    "job_id": "job-123",
                    "lease_id": "lease-123",
                    "lease_expires_at": "2026-08-21T06:00:00Z",
                    "task": "write proof.txt",
                    "workspace": "direct/proof",
                    "origin_session_id": None,
                },
            )
        if request.url.path == "/v1/worker/complete":
            calls["complete"] += 1
            if calls["complete"] == 1:
                return httpx.Response(503, text="lost acknowledgement")
            return httpx.Response(202, json={"accepted": True})
        raise AssertionError(request.url.path)

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://controller.test"
    )
    settings = PollerSettings(
        controller_url="https://controller.test",
        enrollment_token="enrollment-secret",
        worker_id="groken-box",
        state_dir=tmp_path / "state",
        workspace_root=tmp_path / "workspace",
        omo_command="/home/box/.local/bin/omo",
    )
    runner = FakeRunner()
    first = RemotePoller(settings, runner=runner, client=client)

    with pytest.raises(httpx.HTTPStatusError):
        _ = await first.run_once()
    assert (settings.state_dir / "pending-completion.json").is_file()

    restarted = RemotePoller(settings, runner=runner, client=client)
    assert await restarted.run_once() is True
    assert len(runner.calls) == 1
    assert calls["complete"] == 2
    assert not (settings.state_dir / "pending-completion.json").exists()
    await client.aclose()


@pytest.mark.anyio
async def test_poller_quarantines_rejected_pending_completion(tmp_path: Path) -> None:
    calls = {"complete": 0, "lease": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/enroll":
            return httpx.Response(
                200,
                json={
                    "worker_token": "worker-token-with-at-least-32-characters",
                    "model_base_url": "https://models.example.test/v1",
                    "model_api_key": "model-secret",
                    "model": "llm-pool/codex/gpt-5.6-luna",
                },
            )
        if request.url.path == "/v1/worker/lease":
            calls["lease"] += 1
            return httpx.Response(204)
        if request.url.path == "/v1/worker/complete":
            calls["complete"] += 1
            return httpx.Response(409, text="conflicting terminal completion")
        raise AssertionError(request.url.path)

    state_dir = tmp_path / "state"
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://controller.test",
    )
    poller = RemotePoller(
        PollerSettings(
            controller_url="https://controller.test",
            enrollment_token="enrollment-secret",
            worker_id="groken-box",
            state_dir=state_dir,
            workspace_root=tmp_path / "workspace",
            omo_command="/home/box/.local/bin/omo",
        ),
        runner=FakeRunner(),
        client=client,
    )
    assert await poller.run_once() is False
    pending = state_dir / "pending-completion.json"
    _ = pending.write_text(
        CompletionRequest(
            worker_id="groken-box",
            job_id="job-poison",
            lease_id="lease-stale",
            status=ControllerJobStatus.COMPLETED,
            result="done",
        ).model_dump_json()
    )

    assert await poller.run_once() is True
    assert not pending.exists()
    assert (state_dir / "rejected-completion-job-poison.json").is_file()
    assert await poller.run_once() is False
    assert calls == {"complete": 1, "lease": 2}
    await client.aclose()


@pytest.mark.anyio
async def test_poller_refuses_changed_controller_before_sending_token(
    tmp_path: Path,
) -> None:
    requests: list[str] = []

    def first_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/enroll":
            return httpx.Response(
                200,
                json={
                    "worker_token": "worker-token-with-at-least-32-characters",
                    "model_base_url": "https://models.example.test/v1",
                    "model_api_key": "model-secret",
                    "model": "llm-pool/codex/gpt-5.6-luna",
                },
            )
        return httpx.Response(204)

    state_dir = tmp_path / "state"
    first_client = httpx.AsyncClient(
        transport=httpx.MockTransport(first_handler),
        base_url="https://controller-a.test",
    )
    first = RemotePoller(
        PollerSettings(
            controller_url="https://controller-a.test",
            enrollment_token="enrollment-secret",
            worker_id="groken-box",
            state_dir=state_dir,
            workspace_root=tmp_path / "workspace",
            omo_command="/home/box/.local/bin/omo",
        ),
        runner=FakeRunner(),
        client=first_client,
    )
    assert await first.run_once() is False
    await first_client.aclose()

    def second_handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(204)

    second_client = httpx.AsyncClient(
        transport=httpx.MockTransport(second_handler),
        base_url="https://controller-b.test",
    )
    restarted = RemotePoller(
        PollerSettings(
            controller_url="https://controller-b.test",
            enrollment_token="",
            worker_id="groken-box",
            state_dir=state_dir,
            workspace_root=tmp_path / "workspace",
            omo_command="/home/box/.local/bin/omo",
        ),
        runner=FakeRunner(),
        client=second_client,
    )
    with pytest.raises(ValueError, match="controller URL"):
        _ = await restarted.run_once()
    assert requests == []
    await second_client.aclose()
