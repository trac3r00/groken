from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final

from typing_extensions import override

from .env_restore_plan import RestorePlan
from .gateway import BotUpdateError

__all__: Final = ("RestorePlan", "RestoreUnavailableError", "UpdateManifest")


@dataclass(frozen=True, slots=True)
class RestoreUnavailableError(BotUpdateError):
    detail: str

    @override
    def __str__(self) -> str:
        return self.detail


@dataclass(frozen=True, slots=True)
class UpdateManifest:
    manifest_id: str
    captured_at: datetime
    path: Path
