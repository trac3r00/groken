from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from .env_collectors import Inventory
from .env_restore_run import RestoreRunner
from .env_restore_store import JournalStore
from .env_restore_validation import Provider


class ReportClass(StrEnum):
    RESTORED = "restored"
    VERSION_DRIFT = "version-drift"
    MISSING = "missing"
    EXTRA = "extra"
    MANUAL_ACTION = "manual-action"


@dataclass(frozen=True, slots=True)
class ReportItem:
    classification: ReportClass
    provider: Provider
    item: str
    detail: str


@dataclass(frozen=True, slots=True)
class RestoreReport:
    items: tuple[ReportItem, ...]
    exit_code: int

    def count(self, classification: ReportClass) -> int:
        return sum(row.classification is classification for row in self.items)

    @property
    def summary(self) -> str:
        counts = " ".join(f"{kind.value}={self.count(kind)}" for kind in ReportClass)
        return f"restore-report {counts}"


@dataclass(frozen=True, slots=True)
class RestoreOptions:
    retry_manual: bool = False


@dataclass(frozen=True, slots=True)
class RestoreContext:
    store: JournalStore
    runner: RestoreRunner
    recapture: Callable[[], Inventory]
    options: RestoreOptions
    now: Callable[[], datetime] = lambda: datetime.now(UTC)
