import argparse
import os
import socket
from pathlib import Path
from typing import cast

import anyio

from .native_poller import NativePoller, NativePollerSettings
from .poller import PollerSettings, RemotePoller


async def run_pollers(
    omo_settings: PollerSettings,
    native_settings: NativePollerSettings,
) -> None:
    async with anyio.create_task_group() as tasks:
        _ = tasks.start_soon(RemotePoller(omo_settings).run_forever)
        _ = tasks.start_soon(NativePoller(native_settings).run_forever)


def main() -> None:
    parser = argparse.ArgumentParser(prog="groken-poller")
    _ = parser.add_argument("--controller-url", default=os.environ.get("GROKEN_CONTROLLER_URL", ""))
    _ = parser.add_argument(
        "--enrollment-token",
        default=os.environ.get("GROKEN_ENROLLMENT_TOKEN", ""),
    )
    _ = parser.add_argument("--worker-id", default=socket.gethostname())
    _ = parser.add_argument("--state-dir", default="~/.local/state/groken-direct-worker")
    _ = parser.add_argument("--workspace-root", default="/workspace")
    _ = parser.add_argument("--omo-command", default="~/.local/bin/omo")
    _ = parser.add_argument("--poll-interval", default=2, type=float)
    args = parser.parse_args()
    controller_url = cast("str", args.controller_url)
    if not controller_url:
        raise SystemExit("--controller-url or GROKEN_CONTROLLER_URL is required")
    settings = PollerSettings(
        controller_url=controller_url.rstrip("/"),
        enrollment_token=cast("str", args.enrollment_token),
        worker_id=cast("str", args.worker_id),
        state_dir=Path(cast("str", args.state_dir)).expanduser(),
        workspace_root=Path(cast("str", args.workspace_root)).expanduser(),
        omo_command=str(Path(cast("str", args.omo_command)).expanduser()),
        poll_interval_seconds=cast("float", args.poll_interval),
    )
    native_settings = NativePollerSettings(
        controller_url=settings.controller_url,
        worker_id=settings.worker_id,
        state_dir=settings.state_dir,
        workspace_root=settings.workspace_root,
        poll_interval_seconds=settings.poll_interval_seconds,
    )
    anyio.run(run_pollers, settings, native_settings)


if __name__ == "__main__":
    main()
