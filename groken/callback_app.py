import hmac
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Protocol, final

import anyio
from fastapi import Depends, FastAPI, Header, HTTPException, status

from .worker_models import JobRecord


@dataclass(frozen=True)
class CallbackSettings:
    state_file: Path
    bearer_token: str


class EventEmitter(Protocol):
    async def emit(self, record: JobRecord) -> None: ...


@final
class ClawhipEmitter:
    def __init__(self, command: str = "clawhip") -> None:
        self._command: str = command

    async def emit(self, record: JobRecord) -> None:
        fields = [
            "--job_id",
            record.job_id,
            "--status",
            record.status.value,
            "--origin_session_id",
            record.origin_session_id or "",
            "--result",
            (record.result or "")[:4000],
            "--error",
            (record.error or "")[:2000],
        ]
        process = await anyio.run_process(
            [self._command, "emit", "remote.worker.completed", *fields],
            check=False,
        )
        if process.returncode != 0:
            stderr = process.stderr.decode("utf-8", "replace") if process.stderr else ""
            raise RuntimeError(f"clawhip emit failed: {stderr[-1000:]}")


def create_callback_app(
    settings: CallbackSettings,
    *,
    emitter: EventEmitter | None = None,
) -> FastAPI:
    active_emitter = emitter or ClawhipEmitter()
    app = FastAPI(title="Groken Callback Relay", version="1.0.0")
    write_lock = anyio.Lock()

    def require_callback(authorization: Annotated[str | None, Header()] = None) -> None:
        expected = f"Bearer {settings.bearer_token}"
        if not settings.bearer_token or authorization is None or not hmac.compare_digest(
            authorization, expected
        ):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid callback token")

    async def health() -> dict[str, bool]:
        return {"ok": True}

    async def receive(record: JobRecord) -> dict[str, object]:
        settings.state_file.parent.mkdir(parents=True, exist_ok=True)
        async with write_lock:
            descriptor = os.open(
                settings.state_file,
                os.O_WRONLY | os.O_CREAT | os.O_APPEND,
                0o600,
            )
            with os.fdopen(descriptor, "a") as stream:
                _ = stream.write(f"{record.model_dump_json()}\n")
            settings.state_file.chmod(0o600)
        await active_emitter.emit(record)
        return {"accepted": True, "job_id": record.job_id}

    app.add_api_route("/healthz", health, methods=["GET"])
    app.add_api_route(
        "/v1/callbacks",
        receive,
        methods=["POST"],
        status_code=status.HTTP_202_ACCEPTED,
        dependencies=[Depends(require_callback)],
    )
    return app
