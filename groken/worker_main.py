import argparse
import os
from pathlib import Path
from typing import cast

import uvicorn

from .worker_app import WorkerSettings, create_app


def main() -> None:
    parser = argparse.ArgumentParser(prog="groken-worker")
    _ = parser.add_argument("--host", default="127.0.0.1")
    _ = parser.add_argument("--port", default=8765, type=int)
    _ = parser.add_argument("--state-dir", default="~/.local/state/groken-omo-worker")
    _ = parser.add_argument("--workspace-root", default="/workspace")
    _ = parser.add_argument("--omo-command", default="~/.local/bin/omo")
    _ = parser.add_argument("--timeout", default=1800, type=float)
    args = parser.parse_args()
    host = cast("str", args.host)
    port = cast("int", args.port)
    state_dir = cast("str", args.state_dir)
    workspace_root = cast("str", args.workspace_root)
    omo_command = cast("str", args.omo_command)
    timeout_seconds = cast("float", args.timeout)
    settings = WorkerSettings(
        state_dir=Path(state_dir).expanduser(),
        workspace_root=Path(workspace_root).expanduser(),
        bootstrap_token=os.environ.get("GROKEN_WORKER_BOOTSTRAP_TOKEN", ""),
        omo_command=str(Path(omo_command).expanduser()),
        timeout_seconds=timeout_seconds,
    )
    uvicorn.run(create_app(settings), host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
