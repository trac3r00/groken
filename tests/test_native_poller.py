import json
from pathlib import Path
from typing import cast

import httpx
import pytest
from pydantic import AnyHttpUrl, SecretStr

from groken.native_models import NativeOperation
from groken.native_poller import (
    NativeEnrollmentPending,
    NativePoller,
    NativePollerSettings,
)
from groken.poller import PollerConfig
from groken.worker_store import SecretStore, WorkerSecrets


class FakeExecutor:
    def __init__(self) -> None:
        self.operations: list[tuple[NativeOperation, Path]] = []

    async def execute(self, operation: NativeOperation, workspace: Path) -> dict[str, object]:
        self.operations.append((operation, workspace))
        return {"type": operation.type, "exit_code": 0, "stdout_b64": "bmF0aXZl"}


def configure(state_dir: Path) -> None:
    state_dir.mkdir(parents=True)
    _ = (state_dir / "poller.json").write_text(
        PollerConfig(
            controller_url="https://controller.test",
            worker_token="worker-token-with-at-least-32-characters",
        ).model_dump_json()
    )
    SecretStore(state_dir).save(
        WorkerSecrets(
            model_base_url=AnyHttpUrl("https://models.example.test/v1"),
            model_api_key=SecretStr("model-secret"),
            worker_token=SecretStr("worker-token-with-at-least-32-characters"),
            model="llm-pool/codex/gpt-5.6-luna",
        )
    )


@pytest.mark.anyio
async def test_native_poller_reports_enrollment_pending_without_network(tmp_path: Path) -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(500)

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://controller.test"
    )
    poller = NativePoller(
        NativePollerSettings(
            controller_url="https://controller.test",
            worker_id="groken-box",
            state_dir=tmp_path / "missing-state",
            workspace_root=tmp_path / "workspace",
        ),
        executor=FakeExecutor(),
        client=client,
    )

    with pytest.raises(NativeEnrollmentPending):
        _ = await poller.run_once()
    assert requests == []
    await client.aclose()


@pytest.mark.anyio
async def test_native_poller_executes_typed_operation_without_omo(tmp_path: Path) -> None:
    completed: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer worker-token-with-at-least-32-characters"
        if request.url.path == "/v2/worker/lease":
            return httpx.Response(
                200,
                json={
                    "operation_id": "op-1",
                    "lease_id": "lease-1",
                    "lease_expires_at": "2026-08-21T12:00:00Z",
                    "workspace": "native-proof",
                    "operation": {
                        "type": "terminal.exec",
                        "argv": ["/usr/bin/printf", "native"],
                        "cwd": ".",
                        "stdin_b64": "",
                        "env": {},
                        "timeout_ms": 30000,
                    },
                    "origin_session_id": "origin",
                },
            )
        if request.url.path == "/v2/worker/complete":
            completed.append(cast("dict[str, object]", json.loads(request.content)))
            return httpx.Response(202, json={"accepted": True})
        raise AssertionError(request.url.path)

    state_dir = tmp_path / "state"
    configure(state_dir)
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://controller.test"
    )
    executor = FakeExecutor()
    poller = NativePoller(
        NativePollerSettings(
            controller_url="https://controller.test",
            worker_id="groken-box",
            state_dir=state_dir,
            workspace_root=tmp_path / "workspace",
        ),
        executor=executor,
        client=client,
    )

    assert await poller.run_once() is True
    assert len(executor.operations) == 1
    assert executor.operations[0][1] == tmp_path / "workspace" / "native-proof"
    assert completed[0]["lease_id"] == "lease-1"
    assert completed[0]["result"] == {
        "type": "terminal.exec",
        "exit_code": 0,
        "stdout_b64": "bmF0aXZl",
    }
    await client.aclose()
