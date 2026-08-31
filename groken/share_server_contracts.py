"""Typed request and adapter contracts for the share relay."""

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from typing import Protocol, TypeAlias

from pydantic import BaseModel, ConfigDict, Field

from .exec_service import ExecResult
from .share_store import ShareRecord

JsonValue: TypeAlias = object
JsonArgs: TypeAlias = dict[str, object]


class ShareEventFeed(Protocol):
    def next_event(
        self, timeout_s: float | None, *, hold: bool = False
    ) -> dict[str, object]: ...

    def resume(self) -> None: ...


class ShareManager(Protocol):
    def close(self) -> None: ...
    def command(self, method: str, args: JsonArgs | None = None) -> JsonValue: ...
    def send_prompt(self, agent_id: str, text: str) -> dict[str, JsonValue]: ...
    def ask(self, agent_id: str, text: str, timeout_s: float = 600) -> str: ...
    def ask_stream(
        self,
        agent_id: str,
        text: str,
        timeout_s: float = 600,
        idle_s: float = 45,
        on_chunk: Callable[[str], None] | None = None,
    ) -> str: ...
    def transcript_tail(self, agent_id: str) -> list[dict[str, JsonValue]]: ...
    def ensure_sandbox_metadata(self) -> dict[str, JsonValue]: ...
    def events(
        self, channels: list[str] | None = None
    ) -> Iterator[dict[str, JsonValue]]: ...
    def event_subscription(
        self, channels: list[str], timeout_s: float | None
    ) -> AbstractContextManager[ShareEventFeed]: ...


class ShareAuthenticator(Protocol):
    def authenticate(self, token: str) -> ShareRecord | None: ...


class ExecRunner(Protocol):
    async def execute(
        self,
        command: str,
        working_directory: str = "/workspace",
        timeout_ms: int = 15000,
    ) -> ExecResult: ...


class TextRequest(BaseModel):
    model_config = ConfigDict(frozen=True)
    text: str


class AskRequest(TextRequest):
    timeout_s: float = Field(default=600, gt=0)
    idle_s: float = Field(default=45, gt=0)


class CommandRequest(BaseModel):
    model_config = ConfigDict(frozen=True)
    method: str
    args: JsonArgs = Field(default_factory=dict)


class ExecRequest(BaseModel):
    model_config = ConfigDict(frozen=True)
    command: str = Field(min_length=1)
    cwd: str = Field(default="/workspace", min_length=1)
    timeout_ms: int = Field(default=15000, gt=0)


class VncRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


@dataclass(frozen=True, slots=True)
class ShareContext:
    record: ShareRecord
    manager_factory: Callable[[], ShareManager]
    token: str
    _manager: ShareManager | None = field(default=None, repr=False, compare=False)
    _closed: bool = field(default=False, repr=False, compare=False)

    @property
    def manager(self) -> ShareManager:
        manager = self._manager
        if manager is None:
            manager = self.manager_factory()
            object.__setattr__(self, "_manager", manager)
        return manager

    def close(self) -> None:
        if self._closed:
            return
        object.__setattr__(self, "_closed", True)
        manager = self._manager
        if manager is not None:
            manager.close()


@dataclass(frozen=True, slots=True)
class StreamChunk:
    text: str


@dataclass(frozen=True, slots=True)
class StreamDone:
    reply: str


@dataclass(frozen=True, slots=True)
class StreamError:
    detail: str


StreamItem: TypeAlias = StreamChunk | StreamDone | StreamError
