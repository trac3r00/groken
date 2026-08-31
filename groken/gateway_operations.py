from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from .bot_update import (
    GatewayUpdateBackend,
    LocalEnvironmentSnapshots,
    LocalRoutineHooks,
    UpdateConsole,
    UpdateOptions,
    UpdateRuntime,
    run_update,
)
from .env_restore_gateway import (
    GatewayRestoreService,
    RestoreCommandOptions,
    RestoreConsole,
    RestoreGateway,
    production_restore_dependencies,
    run_restore_command,
)
from .gateway import UpdateGateway


def run_gateway_update(
    gateway: UpdateGateway,
    options: UpdateOptions,
    console: UpdateConsole,
) -> None:
    """Run the production update composition with a caller-owned console."""
    root = Path.home() / ".config" / "groken" / "env"
    restore = GatewayRestoreService(
        gateway,
        production_restore_dependencies(root),
    )
    runtime = UpdateRuntime(
        GatewayUpdateBackend(gateway),
        LocalEnvironmentSnapshots(gateway, root, lambda: datetime.now(UTC)),
        LocalRoutineHooks(),
        restore,
    )
    try:
        run_update(options, runtime, console)
    finally:
        restore.close()


def run_gateway_restore(
    gateway: RestoreGateway,
    options: RestoreCommandOptions,
    console: RestoreConsole,
) -> None:
    """Run the production restore composition with a caller-owned console."""
    root = Path.home() / ".config" / "groken" / "env"
    service = GatewayRestoreService(
        gateway,
        production_restore_dependencies(root),
    )
    try:
        run_restore_command(options, service, console)
    finally:
        service.close()
