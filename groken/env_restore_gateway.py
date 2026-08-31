from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, final

from .env_native_runner import NativeEnvironmentRunner
from .env_persistence import CurrentManifestError, read_current_manifest
from .env_restore_contracts import RestorePlan, RestoreUnavailableError, UpdateManifest
from .env_restore_manifest import RestoreManifestError
from .env_restore_plan import RoutineRestore
from .env_restore_report import RestoreReport
from .env_restore_service import (
    NativeRestoreRuntime,
    NativeRestoreService,
    RestoreEnvironment,
)
from .native_client import NativeControllerClient
from .native_wait_models import NativeClientConfigurationError
from .routines import Routine, RoutineEvent, list_routines


class RestoreGateway(Protocol):
    def resolve_agent(self, bot: str | None = None) -> str: ...


class RestoreConsole(Protocol):
    def write(self, line: str) -> None: ...
    def prompt(self, message: str) -> str | None: ...


class RestoreCommandService(Protocol):
    def resolve(self, bot: str | None) -> str: ...
    def manifest(self, bot_id: str) -> UpdateManifest: ...
    def plan(self, bot_id: str, manifest: UpdateManifest) -> RestorePlan: ...
    def restore(
        self,
        bot_id: str,
        manifest: UpdateManifest,
        *,
        retry_manual: bool = False,
    ) -> RestoreReport: ...


@dataclass(frozen=True, slots=True)
class RestoreCommandOptions:
    bot: str | None
    yes: bool
    retry_manual: bool


@dataclass(frozen=True, slots=True)
class GatewayRestoreDependencies:
    root: Path
    routines: Callable[[], tuple[Routine, ...]]
    environment_factory: Callable[[], RestoreEnvironment]
    now: Callable[[], datetime]


def _production_environment() -> RestoreEnvironment:
    try:
        return NativeEnvironmentRunner(NativeControllerClient())
    except NativeClientConfigurationError as exc:
        raise RestoreUnavailableError(f"native restore unavailable: {exc}") from exc


def production_restore_dependencies(root: Path) -> GatewayRestoreDependencies:
    return GatewayRestoreDependencies(
        root,
        list_routines,
        _production_environment,
        lambda: datetime.now(UTC),
    )


@final
class GatewayRestoreService:
    """Resolved-Bot service using the production native completion adapter."""

    def __init__(
        self,
        gateway: RestoreGateway,
        dependencies: GatewayRestoreDependencies,
    ) -> None:
        self._gateway = gateway
        self._dependencies = dependencies
        self._environment: RestoreEnvironment | None = None
        self._native: NativeRestoreService | None = None

    @property
    def report(self) -> RestoreReport | None:
        return self._native.report if self._native is not None else None

    def _service(self) -> NativeRestoreService:
        if self._native is None:
            environment = self._dependencies.environment_factory()
            routines = tuple(
                RoutineRestore(routine.name, (str(routine.entry),))
                for routine in self._dependencies.routines()
                if RoutineEvent.ENV_RESTORE in routine.events
            )
            self._environment = environment
            self._native = NativeRestoreService(
                NativeRestoreRuntime(
                    self._dependencies.root,
                    environment,
                    lambda: routines,
                    self._dependencies.now,
                )
            )
        return self._native

    def resolve(self, bot: str | None) -> str:
        return self._gateway.resolve_agent(bot)

    def manifest(self, bot_id: str) -> UpdateManifest:
        try:
            current = read_current_manifest(self._dependencies.root, bot_id)
        except CurrentManifestError as exc:
            raise RestoreManifestError(str(exc)) from exc
        if current is None:
            raise RestoreManifestError(
                "current manifest is unavailable; run groken bot env capture first"
            )
        return UpdateManifest(current.manifest_id, current.captured_at, current.path)

    def plan(self, bot_id: str, manifest: UpdateManifest) -> RestorePlan:
        return self._service().plan(bot_id, manifest)

    def restore(
        self,
        bot_id: str,
        manifest: UpdateManifest,
        *,
        retry_manual: bool = False,
    ) -> RestoreReport:
        return self._service().restore(
            bot_id,
            manifest,
            retry_manual=retry_manual,
        )

    def close(self) -> None:
        if self._environment is not None:
            self._environment.close()


def run_restore_command(
    options: RestoreCommandOptions,
    service: RestoreCommandService,
    console: RestoreConsole,
) -> None:
    bot_id = service.resolve(options.bot)
    manifest = service.manifest(bot_id)
    plan = service.plan(bot_id, manifest)
    console.write(plan.summary)
    answer = (
        "go"
        if options.yes
        else console.prompt("Type go to restore this Bot environment: ")
    )
    if answer is None or answer.strip() != "go":
        console.write("restore=skipped")
        return
    report = service.restore(
        bot_id,
        manifest,
        retry_manual=options.retry_manual,
    )
    console.write(report.summary)
    console.write(f"restore=completed manifest_id={manifest.manifest_id}")


class TerminalRestoreConsole:
    def write(self, line: str) -> None:
        print(line)

    def prompt(self, message: str) -> str | None:
        try:
            return input(message)
        except EOFError:
            return None


def run_cli_restore(gateway: RestoreGateway, options: RestoreCommandOptions) -> None:
    root = Path.home() / ".config" / "groken" / "env"
    service = GatewayRestoreService(gateway, production_restore_dependencies(root))
    try:
        run_restore_command(options, service, TerminalRestoreConsole())
    finally:
        service.close()
