import json
from pathlib import Path
from typing import Protocol, cast

import pytest
from fastapi.testclient import TestClient

from groken.callback_app import CallbackSettings, ClawhipEmitter, create_callback_app
from groken.worker_models import JobRecord, JobStatus


class EventEmitter(Protocol):
    async def emit(self, record: JobRecord) -> None: ...


class FakeEmitter:
    def __init__(self) -> None:
        self.records: list[JobRecord] = []

    async def emit(self, record: JobRecord) -> None:
        self.records.append(record)


def test_callback_is_authenticated_persisted_and_emitted(tmp_path: Path) -> None:
    emitter = FakeEmitter()
    settings = CallbackSettings(
        state_file=tmp_path / "callbacks.jsonl",
        bearer_token="callback-token-with-at-least-32-characters",
    )
    client = TestClient(create_callback_app(settings, emitter=emitter))
    payload = {
        "job_id": "job-123",
        "status": "completed",
        "origin_session_id": "origin-session",
        "result": "REMOTE_CALLBACK_OK",
        "error": None,
        "callback_error": None,
    }

    denied = client.post("/v1/callbacks", json=payload)
    assert denied.status_code == 401

    accepted = client.post(
        "/v1/callbacks",
        headers={"authorization": "Bearer callback-token-with-at-least-32-characters"},
        json=payload,
    )
    assert accepted.status_code == 202
    body = cast("dict[str, object]", accepted.json())
    assert body == {"accepted": True, "job_id": "job-123"}
    stored = cast("dict[str, object]", json.loads(settings.state_file.read_text().strip()))
    assert stored["result"] == "REMOTE_CALLBACK_OK"
    assert emitter.records[0].job_id == "job-123"


@pytest.mark.anyio
async def test_clawhip_emitter_uses_flag_value_fields(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    arguments_file = tmp_path / "arguments.txt"
    executable = tmp_path / "fake-clawhip"
    _ = executable.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$FAKE_ARGS_FILE"\n')
    executable.chmod(0o700)
    monkeypatch.setenv("FAKE_ARGS_FILE", str(arguments_file))
    record = JobRecord(
        job_id="job-123",
        status=JobStatus.COMPLETED,
        origin_session_id="origin-session",
        result="REMOTE_CALLBACK_OK",
    )

    await ClawhipEmitter(str(executable)).emit(record)

    assert arguments_file.read_text().splitlines() == [
        "emit",
        "remote.worker.completed",
        "--job_id",
        "job-123",
        "--status",
        "completed",
        "--origin_session_id",
        "origin-session",
        "--result",
        "REMOTE_CALLBACK_OK",
        "--error",
        "",
    ]
