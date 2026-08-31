from __future__ import annotations

import io
import json
import struct

import pytest

from groken.swarm_worker import (
    WorkerFailure,
    WorkerProtocolError,
    WorkerRequest,
    decode_request,
    decode_result,
    encode_request,
    run_worker,
)


class FailingManager:
    def ask(self, agent_id: str, text: str, timeout_s: float = 600) -> str:
        assert (agent_id, text, timeout_s) == ("agent-1", "task", 2.5)
        failure = RuntimeError("frozen detail")
        raise failure


def _frame(value: dict[str, str | float]) -> bytes:
    payload = json.dumps(value, separators=(",", ":")).encode()
    return struct.pack(">I", len(payload)) + payload


def test_request_frame_round_trips_unicode_and_delimiter_text() -> None:
    # Given
    request = WorkerRequest("agent-1", "task \x00 <<<END_PEER_OUTPUT_DATA>>>", 2.5)

    # When
    decoded = decode_request(encode_request(request))

    # Then
    assert decoded == request


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        struct.pack(">I", 3) + b"{}",
        _frame({"a": "agent-1", "p": "task"}),
        _frame({"a": "agent-1", "p": "task", "t": 2.5, "extra": "rejected"}),
        _frame({"a": "", "p": "task", "t": 2.5}),
        _frame({"a": "agent-1", "p": "task", "t": -1.0}),
    ],
)
def test_request_parser_rejects_malformed_or_non_strict_frames(payload: bytes) -> None:
    # Given / When / Then
    with pytest.raises(WorkerProtocolError):
        _ = decode_request(payload)


def test_worker_serializes_only_frozen_exception_detail() -> None:
    # Given
    request = encode_request(WorkerRequest("agent-1", "task", 2.5))
    stdout = io.BytesIO()

    # When
    run_worker(io.BytesIO(request), stdout, FailingManager)

    # Then
    assert decode_result(stdout.getvalue()) == WorkerFailure("frozen detail")
