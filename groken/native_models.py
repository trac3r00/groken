import base64
import binascii
import re
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Annotated, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_INLINE_BYTES = 1_048_576
MAX_OUTPUT_BYTES = 1_048_576


def validate_relative_path(value: str) -> str:
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("path must stay inside the selected workspace")
    return value


def validate_base64(value: str) -> str:
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as error:
        raise ValueError("invalid base64 data") from error
    if len(decoded) > MAX_INLINE_BYTES:
        raise ValueError("inline data exceeds 1 MiB")
    return value


class TerminalExec(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")
    type: Literal["terminal.exec"]
    argv: list[str] = Field(min_length=1, max_length=256)
    cwd: str = "."
    stdin_b64: str = ""
    env: dict[str, str] = Field(default_factory=dict)
    timeout_ms: int = Field(default=30_000, ge=1, le=1_800_000)

    @field_validator("cwd")
    @classmethod
    def validate_cwd(cls, value: str) -> str:
        return validate_relative_path(value)

    @field_validator("stdin_b64")
    @classmethod
    def validate_stdin(cls, value: str) -> str:
        return validate_base64(value)

    @model_validator(mode="after")
    def validate_command(self) -> "TerminalExec":
        if not PurePosixPath(self.argv[0]).is_absolute():
            raise ValueError("argv[0] must be an absolute executable path")
        values = [*self.argv, *self.env, *self.env.values()]
        if any("\x00" in value for value in values):
            raise ValueError("command values cannot contain NUL")
        if sum(len(value.encode()) for value in values) > 65_536:
            raise ValueError("command and environment exceed 64 KiB")
        return self


class TerminalShell(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")
    type: Literal["terminal.shell"]
    interpreter: Literal["bash", "posix-sh"] = "bash"
    script: str = Field(min_length=1, max_length=1_048_576)
    cwd: str = "."
    stdin_b64: str = ""
    env: dict[str, str] = Field(default_factory=dict)
    timeout_ms: int = Field(default=30_000, ge=1, le=1_800_000)

    @field_validator("cwd")
    @classmethod
    def validate_cwd(cls, value: str) -> str:
        return validate_relative_path(value)

    @field_validator("stdin_b64")
    @classmethod
    def validate_stdin(cls, value: str) -> str:
        return validate_base64(value)

    @model_validator(mode="after")
    def validate_shell(self) -> "TerminalShell":
        values = [self.script, *self.env, *self.env.values()]
        if any("\x00" in value for value in values):
            raise ValueError("shell values cannot contain NUL")
        return self


class FileRead(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")
    type: Literal["file.read"]
    path: str
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=MAX_INLINE_BYTES, ge=1, le=MAX_INLINE_BYTES)

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return validate_relative_path(value)


class FileWriteMode(StrEnum):
    CREATE = "create"
    REPLACE = "replace"
    UPSERT = "upsert"


class FileWrite(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")
    type: Literal["file.write"]
    path: str
    content_b64: str
    mode: FileWriteMode = FileWriteMode.CREATE
    expected_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    create_parents: bool = False

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return validate_relative_path(value)

    @field_validator("content_b64")
    @classmethod
    def validate_content(cls, value: str) -> str:
        return validate_base64(value)


class FileDelete(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")
    type: Literal["file.delete"]
    path: str
    expected_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    missing_ok: bool = False

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return validate_relative_path(value)


class FileMove(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")
    type: Literal["file.move"]
    source: str
    destination: str
    replace: bool = False
    expected_source_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")

    @field_validator("source", "destination")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return validate_relative_path(value)


class FileGrep(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")
    type: Literal["file.grep"]
    path: str
    pattern: str = Field(min_length=1, max_length=4_096)
    max_matches: int = Field(default=1_000, ge=1, le=10_000)

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return validate_relative_path(value)

    @field_validator("pattern")
    @classmethod
    def validate_pattern(cls, value: str) -> str:
        try:
            _ = re.compile(value)
        except re.error as error:
            raise ValueError("invalid regular expression") from error
        return value


class FileStat(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")
    type: Literal["file.stat"]
    path: str

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return validate_relative_path(value)


class FileList(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")
    type: Literal["file.list"]
    path: str = "."
    limit: int = Field(default=1_000, ge=1, le=10_000)

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return validate_relative_path(value)


class ProcessList(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")
    type: Literal["process.list"]
    query: str | None = Field(default=None, max_length=500)
    limit: int = Field(default=1_000, ge=1, le=10_000)


class ProcessKill(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")
    type: Literal["process.kill"]
    pid: int = Field(ge=2)
    signal: Literal["TERM", "KILL", "INT"] = "TERM"


NativeOperation = Annotated[
    TerminalExec
    | TerminalShell
    | FileRead
    | FileWrite
    | FileDelete
    | FileMove
    | FileGrep
    | FileStat
    | FileList
    | ProcessList
    | ProcessKill,
    Field(discriminator="type"),
]


class NativeOperationRequest(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")
    target: str = Field(min_length=1, max_length=200)
    workspace: str = Field(min_length=1, max_length=300)
    operation: NativeOperation
    origin_session_id: str | None = Field(default=None, max_length=200)

    @field_validator("workspace")
    @classmethod
    def validate_workspace(cls, value: str) -> str:
        return validate_relative_path(value)


class NativeStatus(StrEnum):
    QUEUED = "queued"
    LEASED = "leased"
    COMPLETED = "completed"
    FAILED = "failed"


class NativeLeaseRequest(BaseModel):
    worker_id: str = Field(min_length=1, max_length=200)
    capabilities: list[str] = Field(default_factory=list)


class NativeLeasedOperation(BaseModel):
    operation_id: str
    lease_id: str
    lease_expires_at: datetime
    workspace: str
    operation: NativeOperation
    origin_session_id: str | None = None


class NativeCompletionRequest(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")
    worker_id: str
    operation_id: str
    lease_id: str
    status: NativeStatus
    result: dict[str, object] | None = None
    error: str | None = None


class NativeOperationRecord(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")
    operation_id: str
    status: NativeStatus
    target: str
    workspace: str
    operation: NativeOperation
    origin_session_id: str | None = None
    leased_by: str | None = None
    lease_id: str | None = None
    lease_expires_at: datetime | None = None
    attempt_count: int = 0
    result: dict[str, object] | None = None
    error: str | None = None
    completion_emitted: bool = False
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class NativeAccepted(BaseModel):
    operation_id: str
    status: NativeStatus
