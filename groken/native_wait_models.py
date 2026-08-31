from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator

from .native_models import NativeOperation, NativeStatus, validate_base64


class NativeTerminalOperationRecord(BaseModel):
    """A durable operation record whose status is terminal by construction."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)

    operation_id: str
    status: Literal[NativeStatus.COMPLETED, NativeStatus.FAILED]
    target: str
    workspace: str
    operation: NativeOperation
    origin_session_id: str | None = None
    leased_by: str | None = None
    lease_id: str | None = None
    lease_expires_at: datetime | None = None
    attempt_count: int = 0
    result: dict[str, JsonValue] | None = None
    error: str | None = None
    completion_emitted: bool = False
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class NativeWaitTimeout(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)

    operation_id: str
    status: Literal["timeout"] = "timeout"
    timeout_s: float = Field(gt=0)


class NativeTerminalExecResult(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
    )

    type: Literal["terminal.exec"]
    exit_code: int | None
    stdout_b64: str
    stderr_b64: str
    timed_out: bool
    truncated: bool
    signal: int | None = Field(default=None, ge=1)

    @field_validator("stdout_b64", "stderr_b64")
    @classmethod
    def validate_output(cls, value: str) -> str:
        return validate_base64(value)


@dataclass(frozen=True, slots=True)
class NativeCommandResult:
    argv: tuple[str, ...]
    exit_code: int | None
    stdout: bytes
    stderr: bytes
    timed_out: bool
    truncated: bool
    signal: int | None


@dataclass(frozen=True, slots=True)
class NativeClientConfigurationError(ValueError):
    detail: str

    def __str__(self) -> str:
        return self.detail


@dataclass(frozen=True, slots=True)
class NativeWaitTimeoutError(ValueError):
    operation_id: str
    timeout_s: float

    def __str__(self) -> str:
        return f"native operation {self.operation_id} did not finish within {self.timeout_s:g}s"


@dataclass(frozen=True, slots=True)
class NativeTerminalFailureError(ValueError):
    operation_id: str
    detail: str

    def __str__(self) -> str:
        return f"native operation {self.operation_id} failed: {self.detail}"


@dataclass(frozen=True, slots=True)
class NativeResultError(ValueError):
    operation_id: str
    detail: str

    def __str__(self) -> str:
        return f"native operation {self.operation_id} returned {self.detail}"
