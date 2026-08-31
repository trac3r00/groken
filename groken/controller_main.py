import argparse
import os
from pathlib import Path
from typing import cast

import uvicorn

from .controller_app import ControllerSettings, create_controller_app


def required_env(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise SystemExit(f"{name} is required")
    return value


def main() -> None:
    parser = argparse.ArgumentParser(prog="groken-controller")
    _ = parser.add_argument("--host", default="127.0.0.1")
    _ = parser.add_argument("--port", default=18766, type=int)
    _ = parser.add_argument(
        "--state-dir",
        default="~/.local/state/groken-direct-controller",
    )
    args = parser.parse_args()
    settings = ControllerSettings(
        state_dir=Path(cast("str", args.state_dir)).expanduser(),
        controller_token=required_env("GROKEN_CONTROLLER_TOKEN"),
        enrollment_token=required_env("GROKEN_ENROLLMENT_TOKEN"),
        worker_token=required_env("GROKEN_REMOTE_WORKER_TOKEN"),
        model_base_url=required_env("GROKEN_MODEL_BASE_URL"),
        model_api_key=required_env("GROKEN_MODEL_API_KEY"),
        model=os.environ.get("GROKEN_MODEL", "llm-pool/codex/gpt-5.6-luna"),
        team_alert_team=os.environ.get("GROKEN_TEAM_ALERT_TEAM", ""),
    )
    uvicorn.run(
        create_controller_app(settings),
        host=cast("str", args.host),
        port=cast("int", args.port),
        log_level="info",
    )


if __name__ == "__main__":
    main()
