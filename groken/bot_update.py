from __future__ import annotations

import json
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final, Protocol, assert_never

from typing_extensions import override

from .env_manifest import capture_for_gateway
from .env_persistence import CurrentManifestError, read_current_manifest
from .env_restore_contracts import RestorePlan, RestoreUnavailableError, UpdateManifest
from .env_restore_report import RestoreReport
from .gateway import (
    BotUpdateError,
    UpdateAvailability,
    UpdateGateway,
    UpdateIndeterminateError,
    UpdateReadinessError,
)
from .routines import RoutineEvent, list_routines, run_routine
from .update_backend import GatewayUpdateBackend, UpdateKind, select_update_kind

__all__: Final = (
    "BotUpdateError",
    "GatewayUpdateBackend",
    "RestorePlan",
    "RestoreUnavailableError",
    "UpdateAvailability",
    "UpdateGateway",
    "UpdateIndeterminateError",
    "UpdateKind",
    "UpdateManifest",
    "UpdateReadinessError",
)
_MAX_MANIFEST_AGE: Final = timedelta(hours=24)


@dataclass(frozen=True, slots=True)
class ManifestUnavailableError(BotUpdateError):
    detail: str

    @override
    def __str__(self) -> str:
        return f"manifest unavailable: {self.detail}"


@dataclass(frozen=True, slots=True)
class RoutineFailedError(BotUpdateError):
    routine: str
    event: RoutineEvent
    exit_code: int

    @override
    def __str__(self) -> str:
        return f"routine {self.routine!r} failed for {self.event.value} (exit {self.exit_code})"


@dataclass(frozen=True, slots=True)
class UpdateOptions:
    bot: str | None
    yes: bool
    skip_capture: bool
    readiness_timeout_s: float = 600.0


class UpdateBackend(Protocol):
    def resolve(self, bot: str | None) -> str: ...
    def availability(self, bot_id: str) -> UpdateAvailability: ...
    def subscribe(
        self, bot_id: str, timeout_s: float
    ) -> AbstractContextManager[None]: ...
    def trigger(self, bot_id: str, kind: UpdateKind) -> None: ...
    def wait_ready(self, bot_id: str) -> None: ...


class EnvironmentSnapshots(Protocol):
    def ensure(self, bot_id: str, *, skip_capture: bool) -> UpdateManifest: ...


class RoutineHooks(Protocol):
    def run(self, event: RoutineEvent) -> tuple[str, ...]: ...


class RestorePlanner(Protocol):
    def plan(self, bot_id: str, manifest: UpdateManifest) -> RestorePlan: ...


class RestoreEngine(Protocol):
    def restore(self, bot_id: str, manifest: UpdateManifest) -> RestoreReport: ...


class RestoreService(RestorePlanner, RestoreEngine, Protocol):
    """Task-6 composition seam for planning and executing restore."""


class UpdateConsole(Protocol):
    def write(self, line: str) -> None: ...
    def prompt(self, message: str) -> str | None: ...


@dataclass(frozen=True, slots=True)
class UpdateRuntime:
    backend: UpdateBackend
    snapshots: EnvironmentSnapshots
    routines: RoutineHooks
    restore: RestoreService


class LocalEnvironmentSnapshots:
    def __init__(
        self, gateway: UpdateGateway, root: Path, now: Callable[[], datetime]
    ) -> None:
        self._gateway: UpdateGateway = gateway
        self._root: Path = root
        self._now: Callable[[], datetime] = now

    def _load(self, bot_id: str) -> UpdateManifest | None:
        try:
            current = read_current_manifest(self._root, bot_id)
        except CurrentManifestError as exc:
            raise ManifestUnavailableError(str(exc)) from exc
        if current is None:
            return None
        return UpdateManifest(current.manifest_id, current.captured_at, current.path)

    def ensure(self, bot_id: str, *, skip_capture: bool) -> UpdateManifest:
        reason = "no current manifest"
        try:
            manifest = self._load(bot_id)
        except ManifestUnavailableError as exc:
            manifest = None
            reason = str(exc)
        if manifest is not None:
            age = self._now().astimezone(UTC) - manifest.captured_at
            if timedelta() <= age <= _MAX_MANIFEST_AGE:
                return manifest
            reason = "current manifest is stale"
        if skip_capture:
            raise ManifestUnavailableError(f"{reason}; rerun without --skip-capture")
        _ = capture_for_gateway(self._gateway, bot_id)
        captured = self._load(bot_id)
        if captured is None:
            raise ManifestUnavailableError(
                "capture completed without a current manifest"
            )
        age = self._now().astimezone(UTC) - captured.captured_at
        if not timedelta() <= age <= _MAX_MANIFEST_AGE:
            raise ManifestUnavailableError("capture did not produce a fresh manifest")
        return captured


class LocalRoutineHooks:
    def run(self, event: RoutineEvent) -> tuple[str, ...]:
        names: list[str] = []
        for routine in sorted(list_routines(), key=lambda item: item.name):
            if event not in routine.events:
                continue
            code = run_routine(routine.name, event)
            if code != 0:
                raise RoutineFailedError(routine.name, event, code)
            names.append(routine.name)
        return tuple(names)


def run_update(
    options: UpdateOptions, runtime: UpdateRuntime, console: UpdateConsole
) -> None:
    bot_id = runtime.backend.resolve(options.bot)
    available = runtime.backend.availability(bot_id)
    kind = select_update_kind(available)
    availability_payload: dict[str, object] = {
        "bot": bot_id,
        "hostUpdateAvailable": available.host,
        "imageUpdateAvailable": available.image,
        "selectedUpdate": kind.value if kind is not None else None,
    }
    console.write(json.dumps(availability_payload, sort_keys=True))
    match kind:
        case None:
            console.write("update=not-available")
        case UpdateKind.HOST:
            _ = runtime.routines.run(RoutineEvent.PRE_UPDATE)
            runtime.backend.trigger(bot_id, kind)
            _ = runtime.routines.run(RoutineEvent.POST_UPDATE)
            console.write("update=host-started")
        case UpdateKind.IMAGE:
            manifest = runtime.snapshots.ensure(
                bot_id, skip_capture=options.skip_capture
            )
            _ = runtime.routines.run(RoutineEvent.PRE_UPDATE)
            with runtime.backend.subscribe(bot_id, options.readiness_timeout_s):
                runtime.backend.trigger(bot_id, kind)
                runtime.backend.wait_ready(bot_id)
            _ = runtime.routines.run(RoutineEvent.POST_UPDATE)
            console.write("update=image-ready")
            plan = runtime.restore.plan(bot_id, manifest)
            console.write(plan.summary)
            answer = (
                "go"
                if options.yes
                else console.prompt("Type go to restore this Bot environment: ")
            )
            if answer is None or answer.strip() != "go":
                console.write("restore=skipped")
                return
            report = runtime.restore.restore(bot_id, manifest)
            console.write(report.summary)
            console.write(f"restore=completed manifest_id={manifest.manifest_id}")
        case _ as unreachable:
            assert_never(unreachable)


class _TerminalConsole:
    def write(self, line: str) -> None:
        print(line)

    def prompt(self, message: str) -> str | None:
        try:
            return input(message)
        except EOFError:
            return None


def run_cli_update(gateway: UpdateGateway, options: UpdateOptions) -> None:
    from .env_restore_gateway import (
        GatewayRestoreService,
        production_restore_dependencies,
    )

    root = Path.home() / ".config" / "groken" / "env"
    restore = GatewayRestoreService(gateway, production_restore_dependencies(root))
    runtime = UpdateRuntime(
        GatewayUpdateBackend(gateway),
        LocalEnvironmentSnapshots(gateway, root, lambda: datetime.now(UTC)),
        LocalRoutineHooks(),
        restore,
    )
    try:
        run_update(options, runtime, _TerminalConsole())
    finally:
        restore.close()
