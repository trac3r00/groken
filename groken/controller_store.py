import hashlib
import json
import os
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import final

from .controller_models import (
    CompletionRequest,
    ControllerJobRecord,
    ControllerJobRequest,
    ControllerJobStatus,
    IdempotencyRecord,
    LeasedJob,
)


def utc_now() -> datetime:
    return datetime.now(UTC)


@final
class ControllerStore:
    def __init__(
        self,
        state_dir: Path,
        *,
        now: Callable[[], datetime] = utc_now,
        lease_seconds: int = 3600,
    ) -> None:
        self._jobs_dir: Path = state_dir / "jobs"
        self._idempotency_dir: Path = state_dir / "idempotency"
        self._jobs_dir.mkdir(parents=True, exist_ok=True)
        self._idempotency_dir.mkdir(parents=True, exist_ok=True)
        self._now = now
        self._lease_duration = timedelta(seconds=lease_seconds)

    def create(self, job_id: str, request: ControllerJobRequest) -> ControllerJobRecord:
        record = ControllerJobRecord(
            job_id=job_id,
            status=ControllerJobStatus.QUEUED,
            task=request.task,
            workspace=request.workspace,
            origin_session_id=request.origin_session_id,
            created_at=self._now(),
            updated_at=self._now(),
        )
        self.write(record)
        return record

    def create_idempotent(
        self,
        job_id: str,
        idempotency_key: str,
        request: ControllerJobRequest,
    ) -> ControllerJobRecord:
        request_payload = json.dumps(
            request.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        request_digest = hashlib.sha256(request_payload).hexdigest()
        key_digest = hashlib.sha256(idempotency_key.encode()).hexdigest()
        path = self._idempotency_dir / f"{key_digest}.json"
        if path.is_file():
            saved = IdempotencyRecord.model_validate_json(path.read_text())
            if saved.request_digest != request_digest:
                raise ValueError("idempotency key was reused with a different request")
            return self.read(saved.job_id)
        record = self.create(job_id, request)
        self._write_private(
            path,
            IdempotencyRecord(
                request_digest=request_digest,
                job_id=record.job_id,
            ).model_dump_json(indent=2),
        )
        return record

    def lease_next(self, worker_id: str) -> LeasedJob | None:
        records = self._records()
        now = self._now()
        for record in records:
            if (
                record.status is ControllerJobStatus.LEASED
                and record.leased_by == worker_id
                and record.lease_id is not None
                and record.lease_expires_at is not None
                and record.lease_expires_at > now
            ):
                return self._leased_job(record)
        for record in records:
            if (
                record.status is ControllerJobStatus.LEASED
                and (
                    record.lease_id is None
                    or record.lease_expires_at is None
                    or record.lease_expires_at <= now
                )
            ):
                record.status = ControllerJobStatus.QUEUED
                record.leased_by = None
                record.lease_id = None
                record.lease_expires_at = None
                record.updated_at = now
                self.write(record)
        for record in records:
            if record.status is not ControllerJobStatus.QUEUED:
                continue
            record.status = ControllerJobStatus.LEASED
            record.leased_by = worker_id
            record.lease_id = str(uuid.uuid4())
            record.lease_expires_at = now + self._lease_duration
            record.attempt_count += 1
            record.updated_at = now
            self.write(record)
            return self._leased_job(record)
        return None

    def complete(self, request: CompletionRequest) -> ControllerJobRecord:
        if request.status not in {ControllerJobStatus.COMPLETED, ControllerJobStatus.FAILED}:
            raise ValueError("completion status must be completed or failed")
        record = self.read(request.job_id)
        if record.status in {ControllerJobStatus.COMPLETED, ControllerJobStatus.FAILED}:
            if self._same_completion(record, request):
                return record
            raise ValueError("conflicting terminal completion")
        if (
            record.status is not ControllerJobStatus.LEASED
            or record.leased_by != request.worker_id
            or record.lease_id != request.lease_id
        ):
            raise ValueError("job is not held by this lease")
        record.status = request.status
        record.result = request.result
        record.error = request.error
        record.updated_at = self._now()
        self.write(record)
        return record

    def mark_completion_emitted(self, job_id: str) -> ControllerJobRecord:
        record = self.read(job_id)
        record.completion_emitted = True
        record.updated_at = self._now()
        self.write(record)
        return record

    def read(self, job_id: str) -> ControllerJobRecord:
        return self._read_path(self._path(job_id))

    def write(self, record: ControllerJobRecord) -> None:
        self._write_private(self._path(record.job_id), record.model_dump_json(indent=2))

    @staticmethod
    def _write_private(path: Path, content: str) -> None:
        temporary = path.with_suffix(".tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            _ = stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)

    def _records(self) -> list[ControllerJobRecord]:
        return sorted(
            (self._read_path(path) for path in self._jobs_dir.glob("*.json")),
            key=lambda record: record.created_at,
        )

    @staticmethod
    def _leased_job(record: ControllerJobRecord) -> LeasedJob:
        if record.lease_id is None or record.lease_expires_at is None:
            raise ValueError("leased record is missing lease metadata")
        return LeasedJob(
            job_id=record.job_id,
            lease_id=record.lease_id,
            lease_expires_at=record.lease_expires_at,
            task=record.task,
            workspace=record.workspace,
            origin_session_id=record.origin_session_id,
        )

    @staticmethod
    def _same_completion(record: ControllerJobRecord, request: CompletionRequest) -> bool:
        return (
            record.leased_by == request.worker_id
            and record.lease_id == request.lease_id
            and record.status is request.status
            and record.result == request.result
            and record.error == request.error
        )

    def _read_path(self, path: Path) -> ControllerJobRecord:
        return ControllerJobRecord.model_validate_json(path.read_text())

    def _path(self, job_id: str) -> Path:
        return self._jobs_dir / f"{job_id}.json"
