import hashlib
import json
import os
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import final

from .controller_models import IdempotencyRecord
from .native_models import (
    NativeCompletionRequest,
    NativeLeasedOperation,
    NativeOperationRecord,
    NativeOperationRequest,
    NativeStatus,
)


def utc_now() -> datetime:
    return datetime.now(UTC)


@final
class NativeStore:
    def __init__(
        self,
        state_dir: Path,
        *,
        now: Callable[[], datetime] = utc_now,
        lease_seconds: int = 3600,
    ) -> None:
        self._operations_dir = state_dir / "native-operations"
        self._idempotency_dir = state_dir / "native-idempotency"
        self._operations_dir.mkdir(parents=True, exist_ok=True)
        self._idempotency_dir.mkdir(parents=True, exist_ok=True)
        self._now = now
        self._lease_duration = timedelta(seconds=lease_seconds)

    def create_idempotent(
        self,
        operation_id: str,
        idempotency_key: str,
        request: NativeOperationRequest,
    ) -> NativeOperationRecord:
        payload = json.dumps(
            request.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        ).encode()
        request_digest = hashlib.sha256(payload).hexdigest()
        key_digest = hashlib.sha256(idempotency_key.encode()).hexdigest()
        path = self._idempotency_dir / f"{key_digest}.json"
        if path.is_file():
            saved = IdempotencyRecord.model_validate_json(path.read_text())
            if saved.request_digest != request_digest:
                raise ValueError("idempotency key was reused with a different request")
            return self.read(saved.job_id)
        now = self._now()
        record = NativeOperationRecord(
            operation_id=operation_id,
            status=NativeStatus.QUEUED,
            target=request.target,
            workspace=request.workspace,
            operation=request.operation,
            origin_session_id=request.origin_session_id,
            created_at=now,
            updated_at=now,
        )
        self.write(record)
        self._write_private(
            path,
            IdempotencyRecord(
                request_digest=request_digest,
                job_id=operation_id,
            ).model_dump_json(indent=2),
        )
        return record

    def lease_next(self, worker_id: str, capabilities: set[str]) -> NativeLeasedOperation | None:
        records = self._records()
        now = self._now()
        for record in records:
            if (
                record.status is NativeStatus.LEASED
                and record.leased_by == worker_id
                and record.lease_id is not None
                and record.lease_expires_at is not None
                and record.lease_expires_at > now
            ):
                return self._leased(record)
        for record in records:
            if record.status is NativeStatus.LEASED and (
                record.lease_id is None
                or record.lease_expires_at is None
                or record.lease_expires_at <= now
            ):
                record.status = NativeStatus.QUEUED
                record.leased_by = None
                record.lease_id = None
                record.lease_expires_at = None
                record.updated_at = now
                self.write(record)
        for record in records:
            operation_family = record.operation.type.split(".", maxsplit=1)[0]
            required = {
                "file": "files",
                "process": "processes",
            }.get(operation_family, operation_family)
            if (
                record.status is not NativeStatus.QUEUED
                or record.target != worker_id
                or required not in capabilities
            ):
                continue
            record.status = NativeStatus.LEASED
            record.leased_by = worker_id
            record.lease_id = str(uuid.uuid4())
            record.lease_expires_at = now + self._lease_duration
            record.attempt_count += 1
            record.updated_at = now
            self.write(record)
            return self._leased(record)
        return None

    def complete(self, request: NativeCompletionRequest) -> NativeOperationRecord:
        if request.status not in {NativeStatus.COMPLETED, NativeStatus.FAILED}:
            raise ValueError("completion status must be completed or failed")
        record = self.read(request.operation_id)
        if record.status in {NativeStatus.COMPLETED, NativeStatus.FAILED}:
            if self._same_completion(record, request):
                return record
            raise ValueError("conflicting terminal completion")
        if (
            record.status is not NativeStatus.LEASED
            or record.leased_by != request.worker_id
            or record.lease_id != request.lease_id
        ):
            raise ValueError("operation is not held by this lease")
        record.status = request.status
        record.result = request.result
        record.error = request.error
        record.updated_at = self._now()
        self.write(record)
        return record

    def mark_emitted(self, operation_id: str) -> NativeOperationRecord:
        record = self.read(operation_id)
        record.completion_emitted = True
        record.updated_at = self._now()
        self.write(record)
        return record

    def read(self, operation_id: str) -> NativeOperationRecord:
        return NativeOperationRecord.model_validate_json(self._path(operation_id).read_text())

    def write(self, record: NativeOperationRecord) -> None:
        self._write_private(self._path(record.operation_id), record.model_dump_json(indent=2))

    def _records(self) -> list[NativeOperationRecord]:
        return sorted(
            (
                NativeOperationRecord.model_validate_json(path.read_text())
                for path in self._operations_dir.glob("*.json")
            ),
            key=lambda record: record.created_at,
        )

    @staticmethod
    def _leased(record: NativeOperationRecord) -> NativeLeasedOperation:
        if record.lease_id is None or record.lease_expires_at is None:
            raise ValueError("leased operation is missing lease metadata")
        return NativeLeasedOperation(
            operation_id=record.operation_id,
            lease_id=record.lease_id,
            lease_expires_at=record.lease_expires_at,
            workspace=record.workspace,
            operation=record.operation,
            origin_session_id=record.origin_session_id,
        )

    @staticmethod
    def _same_completion(
        record: NativeOperationRecord, request: NativeCompletionRequest
    ) -> bool:
        return (
            record.leased_by == request.worker_id
            and record.lease_id == request.lease_id
            and record.status is request.status
            and record.result == request.result
            and record.error == request.error
        )

    @staticmethod
    def _write_private(path: Path, content: str) -> None:
        temporary = path.with_suffix(".tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            _ = stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)

    def _path(self, operation_id: str) -> Path:
        return self._operations_dir / f"{operation_id}.json"
