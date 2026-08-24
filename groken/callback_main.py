import argparse
import os
from pathlib import Path
from typing import cast

import uvicorn

from .callback_app import CallbackSettings, create_callback_app


def main() -> None:
    parser = argparse.ArgumentParser(prog="groken-callback")
    _ = parser.add_argument("--host", default="127.0.0.1")
    _ = parser.add_argument("--port", default=18765, type=int)
    _ = parser.add_argument(
        "--state-file",
        default="~/.local/state/groken-omo-controller/callbacks.jsonl",
    )
    args = parser.parse_args()
    host = cast("str", args.host)
    port = cast("int", args.port)
    state_file = cast("str", args.state_file)
    token = os.environ.get("GROKEN_CALLBACK_TOKEN", "")
    if not token:
        raise SystemExit("GROKEN_CALLBACK_TOKEN is required")
    settings = CallbackSettings(
        state_file=Path(state_file).expanduser(),
        bearer_token=token,
    )
    uvicorn.run(create_callback_app(settings), host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
