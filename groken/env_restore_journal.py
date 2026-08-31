from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TypeAlias

JsonValue: TypeAlias = (
    str | int | float | bool | None | list["JsonValue"] | dict[str, "JsonValue"]
)


class JournalState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    MANUAL = "manual"


@dataclass(frozen=True, slots=True)
class JournalEntry:
    key: str
    item: str
    argv: tuple[str, ...]
    state: JournalState
    attempts: int
    idempotency_key: str | None
    started_at: str | None
    ended_at: str | None
    exit_code: int | None
    signal: int | None
    truncated: bool
    error: str | None


@dataclass(frozen=True, slots=True)
class RestoreJournal:
    bot_id: str
    manifest_id: str
    operations: tuple[JournalEntry, ...]

    def find(self, key: str) -> JournalEntry:
        return next(row for row in self.operations if row.key == key)
