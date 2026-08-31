"""Fresh-interpreter worker for one isolated swarm ask."""

from __future__ import annotations

import json
import math
import struct
import sys
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import BinaryIO, Final, Protocol, assert_never

_HEADER: Final = struct.Struct(">I")
_MAX_FRAME_BYTES: Final = 64 * 1024 * 1024


class WorkerProtocolError(Exception):
    """Raised when a worker frame violates the strict JSON contract."""

    reason: str

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class AskManager(Protocol):
    def ask(self, agent_id: str, text: str, timeout_s: float = 600) -> str: ...


@dataclass(frozen=True, slots=True)
class WorkerRequest:
    agent_id: str
    prompt: str
    timeout_s: float


@dataclass(frozen=True, slots=True)
class WorkerAnswer:
    text: str


@dataclass(frozen=True, slots=True)
class WorkerFailure:
    error: str


WorkerResult = WorkerAnswer | WorkerFailure


def _encode_payload(payload: bytes) -> bytes:
    if len(payload) > _MAX_FRAME_BYTES:
        raise WorkerProtocolError("worker frame exceeds size limit")
    return _HEADER.pack(len(payload)) + payload


def _decode_payload(frame: bytes) -> bytes:
    if len(frame) < _HEADER.size:
        raise WorkerProtocolError("worker frame header is missing")
    (length,) = _HEADER.unpack(frame[: _HEADER.size])
    if length > _MAX_FRAME_BYTES:
        raise WorkerProtocolError("worker frame exceeds size limit")
    payload = frame[_HEADER.size :]
    if len(payload) != length:
        raise WorkerProtocolError("worker frame length does not match payload")
    return payload


def encode_request(request: WorkerRequest) -> bytes:
    """Encode one strict length-prefixed worker request."""
    payload = json.dumps(
        {"a": request.agent_id, "p": request.prompt, "t": request.timeout_s},
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()
    return _encode_payload(payload)


def decode_request(frame: bytes) -> WorkerRequest:
    """Parse one strict length-prefixed worker request."""
    try:
        raw = json.loads(_decode_payload(frame))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkerProtocolError("worker request is not valid JSON") from exc
    if not isinstance(raw, dict) or set(raw) != {"a", "p", "t"}:
        raise WorkerProtocolError("worker request fields are invalid")
    agent_id = raw["a"]
    prompt = raw["p"]
    timeout_s = raw["t"]
    if not isinstance(agent_id, str) or not agent_id:
        raise WorkerProtocolError("worker agent_id must be non-empty")
    if not isinstance(prompt, str):
        raise WorkerProtocolError("worker prompt must be text")
    if (
        isinstance(timeout_s, bool)
        or not isinstance(timeout_s, (int, float))
        or not math.isfinite(timeout_s)
        or timeout_s <= 0
    ):
        raise WorkerProtocolError("worker timeout must be positive and finite")
    return WorkerRequest(agent_id, prompt, float(timeout_s))


def encode_result(result: WorkerResult) -> bytes:
    """Encode one strict length-prefixed worker result."""
    match result:
        case WorkerAnswer(text=text):
            payload = {"a": text}
        case WorkerFailure(error=error):
            payload = {"e": error}
        case _ as unreachable:
            assert_never(unreachable)
    return _encode_payload(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    )


def decode_result(frame: bytes) -> WorkerResult:
    """Parse one strict length-prefixed worker result."""
    try:
        raw = json.loads(_decode_payload(frame))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkerProtocolError("worker result is not valid JSON") from exc
    if not isinstance(raw, dict) or len(raw) != 1:
        raise WorkerProtocolError("worker result fields are invalid")
    if set(raw) == {"a"} and isinstance(raw["a"], str):
        return WorkerAnswer(raw["a"])
    if set(raw) == {"e"} and isinstance(raw["e"], str):
        return WorkerFailure(raw["e"])
    raise WorkerProtocolError("worker result variant is invalid")


def run_worker(
    stdin: BinaryIO,
    stdout: BinaryIO,
    manager_factory: Callable[[], AskManager],
) -> None:
    """Read one request, perform one ask, and write one result."""
    request = decode_request(stdin.read())
    manager = manager_factory()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            manager.ask,
            request.agent_id,
            request.prompt,
            request.timeout_s,
        )
        match future.exception():
            case None:
                result: WorkerResult = WorkerAnswer(future.result())
            case BaseException() as exc:
                result = WorkerFailure(str(exc) or type(exc).__name__)
            case _ as unreachable:
                assert_never(unreachable)
    stdout.write(encode_result(result))
    stdout.flush()


def main() -> None:
    """Run the strict worker protocol over standard input and output."""
    from .gateway import GatewayManager

    try:
        run_worker(sys.stdin.buffer, sys.stdout.buffer, GatewayManager)
    except WorkerProtocolError as exc:
        sys.stdout.buffer.write(encode_result(WorkerFailure(str(exc))))
        sys.stdout.buffer.flush()


if __name__ == "__main__":
    main()
