from datetime import UTC, datetime
from enum import StrEnum
from typing import ClassVar

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field


class ControllerJobStatus(StrEnum):
    QUEUED = "queued"
    LEASED = "leased"
    COMPLETED = "completed"
    FAILED = "failed"


class EnrollmentRequest(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")
    worker_id: str = Field(min_length=1, max_length=200)


class EnrollmentResponse(BaseModel):
    worker_token: str
    model_base_url: AnyHttpUrl
    model_api_key: str
    model: str


class ControllerJobRequest(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")
    task: str = Field(min_length=1, max_length=100_000)
    workspace: str = Field(min_length=1, max_length=300)
    origin_session_id: str | None = Field(default=None, max_length=200)


class LeaseRequest(BaseModel):
    worker_id: str = Field(min_length=1, max_length=200)


class LeasedJob(BaseModel):
    job_id: str
    lease_id: str
    lease_expires_at: datetime
    task: str
    workspace: str
    origin_session_id: str | None = None


class CompletionRequest(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")
    worker_id: str = Field(min_length=1, max_length=200)
    job_id: str
    lease_id: str
    status: ControllerJobStatus
    result: str | None = None
    error: str | None = None


class ControllerJobRecord(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")
    job_id: str
    status: ControllerJobStatus
    task: str
    workspace: str
    origin_session_id: str | None = None
    leased_by: str | None = None
    lease_id: str | None = None
    lease_expires_at: datetime | None = None
    attempt_count: int = 0
    result: str | None = None
    error: str | None = None
    completion_emitted: bool = False
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ControllerJobAccepted(BaseModel):
    job_id: str
    status: ControllerJobStatus


class IdempotencyRecord(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")
    request_digest: str
    job_id: str
