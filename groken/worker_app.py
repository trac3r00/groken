import hmac
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Protocol

import httpx
from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, status

from .worker_models import (
    BootstrapRequest,
    JobAccepted,
    JobExecution,
    JobRecord,
    JobRequest,
    JobStatus,
)
from .worker_runner import OmoRunner, resolve_workspace
from .worker_store import JobStore, SecretStore, WorkerSecrets


@dataclass(frozen=True)
class WorkerSettings:
    state_dir: Path
    workspace_root: Path
    bootstrap_token: str
    omo_command: str
    timeout_seconds: float = 1800

    @property
    def secrets_file(self) -> Path:
        return self.state_dir / "secrets.json"


class Runner(Protocol):
    async def run(self, job_id: str, request: JobRequest, workspace: Path) -> JobExecution: ...


class CallbackSender(Protocol):
    async def send(self, request: JobRequest, payload: dict[str, object]) -> None: ...


class HttpCallbackSender:
    async def send(self, request: JobRequest, payload: dict[str, object]) -> None:
        if request.callback_url is None:
            return
        headers: dict[str, str] = {}
        if request.callback_token is not None:
            headers["authorization"] = f"Bearer {request.callback_token.get_secret_value()}"
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(str(request.callback_url), headers=headers, json=payload)
            _ = response.raise_for_status()


def create_app(
    settings: WorkerSettings,
    *,
    runner: Runner | None = None,
    callback_sender: CallbackSender | None = None,
) -> FastAPI:
    secret_store = SecretStore(settings.state_dir)
    job_store = JobStore(settings.state_dir)
    active_runner = runner or OmoRunner(
        omo_command=settings.omo_command,
        state_dir=settings.state_dir,
        secret_store=secret_store,
        timeout_seconds=settings.timeout_seconds,
    )
    callbacks = callback_sender or HttpCallbackSender()
    app = FastAPI(title="Groken OMO Worker", version="1.0.0")

    def require_worker(authorization: Annotated[str | None, Header()] = None) -> None:
        if not secret_store.configured():
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "worker is not configured")
        expected = f"Bearer {secret_store.load().worker_token.get_secret_value()}"
        if authorization is None or not hmac.compare_digest(authorization, expected):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid worker token")

    def require_bootstrap(x_bootstrap_token: Annotated[str | None, Header()] = None) -> None:
        if not settings.bootstrap_token or x_bootstrap_token is None or not hmac.compare_digest(
            x_bootstrap_token, settings.bootstrap_token
        ):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid bootstrap token")

    async def health() -> dict[str, object]:
        return {"ok": True, "configured": secret_store.configured()}

    async def bootstrap(request: BootstrapRequest) -> dict[str, bool]:
        if secret_store.configured():
            raise HTTPException(status.HTTP_409_CONFLICT, "worker is already configured")
        secret_store.save(
            WorkerSecrets(
                model_base_url=request.model_base_url,
                model_api_key=request.model_api_key,
                worker_token=request.worker_token,
                model=request.model,
            )
        )
        return {"configured": True}

    async def execute(job_id: str, request: JobRequest, workspace: Path) -> None:
        record = JobRecord(
            job_id=job_id,
            status=JobStatus.RUNNING,
            origin_session_id=request.origin_session_id,
        )
        job_store.write(record)
        try:
            execution = await active_runner.run(job_id, request, workspace)
            record.status = JobStatus.COMPLETED
            record.result = execution.result
        except Exception as error:  # noqa: BLE001 - job boundary records typed failure state
            record.status = JobStatus.FAILED
            record.error = str(error)
        job_store.write(record)
        payload = record.model_dump(mode="json")
        try:
            await callbacks.send(request, payload)
        except (httpx.HTTPError, OSError) as error:
            record.callback_error = str(error)
            job_store.write(record)

    async def submit(request: JobRequest, background_tasks: BackgroundTasks) -> JobAccepted:
        try:
            workspace = resolve_workspace(settings.workspace_root, request.workspace)
        except ValueError as error:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(error)) from error
        job_id = str(uuid.uuid4())
        job_store.write(
            JobRecord(
                job_id=job_id,
                status=JobStatus.ACCEPTED,
                origin_session_id=request.origin_session_id,
            )
        )
        background_tasks.add_task(execute, job_id, request, workspace)
        return JobAccepted(job_id=job_id, status=JobStatus.ACCEPTED)

    async def job_status(job_id: str) -> JobRecord:
        try:
            return job_store.read(job_id)
        except FileNotFoundError as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown job") from error

    app.add_api_route("/healthz", health, methods=["GET"])
    app.add_api_route(
        "/v1/bootstrap",
        bootstrap,
        methods=["POST"],
        status_code=status.HTTP_201_CREATED,
        dependencies=[Depends(require_bootstrap)],
    )
    app.add_api_route(
        "/v1/jobs",
        submit,
        methods=["POST"],
        response_model=JobAccepted,
        status_code=status.HTTP_202_ACCEPTED,
        dependencies=[Depends(require_worker)],
    )
    app.add_api_route(
        "/v1/jobs/{job_id}",
        job_status,
        methods=["GET"],
        response_model=JobRecord,
        dependencies=[Depends(require_worker)],
    )
    return app
