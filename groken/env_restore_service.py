from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol, final

from typing_extensions import override

from .env_collectors import Inventory
from .env_native_runner import CapturePhase
from .env_restore import (
    RestoreContext,
    RestoreOptions,
    RestoreReport,
    RestoreRequest,
    RestoreRunner,
    RoutineRestore,
    execute_restore,
    plan_restore,
)
from .env_restore_contracts import RestoreUnavailableError, UpdateManifest
from .env_restore_manifest import LoadedInventory, load_inventory
from .env_restore_plan import RestorePlan
from .env_restore_store import JournalStore
from .gateway import BotUpdateError


class RestoreEnvironment(RestoreRunner, Protocol):
    def capture(self, manifest_id: str, phase: CapturePhase) -> Inventory: ...
    def brewfile_path(self, loaded: LoadedInventory) -> Path | None: ...
    def prepare(self, loaded: LoadedInventory) -> None: ...
    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class NativeRestoreRuntime:
    root: Path
    environment: RestoreEnvironment
    routines: Callable[[], tuple[RoutineRestore, ...]]
    now: Callable[[], datetime]


@dataclass(frozen=True, slots=True)
class RestoreExecutionError(BotUpdateError):
    summary: str

    @override
    def __str__(self) -> str:
        return self.summary


@final
class NativeRestoreService:
    """Task-5 planner/engine composition over one native environment adapter."""

    def __init__(self, runtime: NativeRestoreRuntime) -> None:
        self._runtime = runtime
        self._prepared: tuple[str, str, RestorePlan, LoadedInventory] | None = None
        self.report: RestoreReport | None = None

    def plan(self, bot_id: str, manifest: UpdateManifest) -> RestorePlan:
        loaded = load_inventory(manifest.path, bot_id, manifest.manifest_id)
        current = self._runtime.environment.capture(
            manifest.manifest_id,
            CapturePhase.PLAN,
        )
        core = plan_restore(
            RestoreRequest(
                loaded.inventory,
                current,
                loaded.brewfile_path,
                self._runtime.routines(),
                self._runtime.environment.brewfile_path(loaded),
            )
        )
        self._prepared = (bot_id, manifest.manifest_id, core, loaded)
        return core

    def restore(
        self,
        bot_id: str,
        manifest: UpdateManifest,
        *,
        retry_manual: bool = False,
    ) -> RestoreReport:
        prepared = self._prepared
        if prepared is None or prepared[:2] != (bot_id, manifest.manifest_id):
            raise RestoreUnavailableError(
                "restore plan is unavailable or belongs to a different manifest"
            )
        self._runtime.environment.prepare(prepared[3])
        phases = iter((CapturePhase.PRE_RESTORE, CapturePhase.POST_RESTORE))

        def recapture() -> Inventory:
            return self._runtime.environment.capture(manifest.manifest_id, next(phases))

        context = RestoreContext(
            JournalStore(self._runtime.root, bot_id, manifest.manifest_id),
            self._runtime.environment,
            recapture,
            RestoreOptions(retry_manual),
            self._runtime.now,
        )
        self.report = execute_restore(prepared[2], context)
        if self.report.exit_code != 0:
            raise RestoreExecutionError(self.report.summary)
        return self.report
