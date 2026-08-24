from enum import StrEnum
from typing import ClassVar

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, SecretStr


class JobStatus(StrEnum):
    ACCEPTED = "accepted"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class BootstrapRequest(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    model_base_url: AnyHttpUrl
    model_api_key: SecretStr = Field(min_length=1)
    worker_token: SecretStr = Field(min_length=32)
    model: str = Field(min_length=1, max_length=200)


class JobRequest(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    task: str = Field(min_length=1, max_length=100_000)
    workspace: str = Field(min_length=1, max_length=300)
    origin_session_id: str | None = Field(default=None, max_length=200)
    callback_url: AnyHttpUrl | None = None
    callback_token: SecretStr | None = Field(default=None, min_length=32)


class JobExecution(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    result: str
    exit_code: int


class JobRecord(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    job_id: str
    status: JobStatus
    origin_session_id: str | None = None
    result: str | None = None
    error: str | None = None
    callback_error: str | None = None


class JobAccepted(BaseModel):
    job_id: str
    status: JobStatus
