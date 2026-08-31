from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class RestoreRunRequest:
    manifest_id: str
    operation_key: str
    attempt: int
    idempotency_key: str
    argv: tuple[str, ...]
    stdin: bytes
    timeout_ms: int


@dataclass(frozen=True, slots=True)
class RestoreRunResult:
    argv: tuple[str, ...]
    exit_code: int | None
    stdout: bytes
    stderr: bytes
    timed_out: bool
    truncated: bool
    signal: int | None


class RestorePendingError(Exception):
    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail: str = detail


class RestoreRunner(Protocol):
    def run_restore(self, request: RestoreRunRequest) -> RestoreRunResult: ...


def restore_idempotency_key(
    manifest_id: str,
    operation_key: str,
    attempt: int,
) -> str:
    digest = hashlib.sha256()
    for value in (manifest_id, operation_key, str(attempt)):
        encoded = value.encode()
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return f"groken-env-restore:{digest.hexdigest()}"
