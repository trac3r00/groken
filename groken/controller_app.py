import hmac
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Annotated, Protocol, final

import anyio
from fastapi import Depends, FastAPI, Header, HTTPException, Response, status

from .callback_app import ClawhipEmitter
from .controller_models import (
    CompletionRequest,
    ControllerJobAccepted,
    ControllerJobRecord,
    ControllerJobRequest,
    ControllerJobStatus,
    EnrollmentRequest,
    EnrollmentResponse,
    LeasedJob,
    LeaseRequest,
)
from .controller_store import ControllerStore
from .native_controller import (
    NativeControllerSettings,
    NativeEmitter,
    create_native_router,
)
from .worker_models import JobRecord, JobStatus


@dataclass(frozen=True)
class ControllerSettings:
    state_dir: Path
    controller_token: str
    enrollment_token: str
    worker_token: str
    model_base_url: str
    model_api_key: str
    model: str
    lease_seconds: int = 3600


class CompletionEmitter(Protocol):
    async def emit(self, record: ControllerJobRecord) -> None: ...


@final
class ClawhipCompletionEmitter:
    def __init__(self) -> None:
        self._emitter = ClawhipEmitter()

    async def emit(self, record: ControllerJobRecord) -> None:
        status_value = (
            JobStatus.COMPLETED
            if record.status is ControllerJobStatus.COMPLETED
            else JobStatus.FAILED
        )
        await self._emitter.emit(
            JobRecord(
                job_id=record.job_id,
                status=status_value,
                origin_session_id=record.origin_session_id,
                result=record.result,
                error=record.error,
            )
        )


def create_controller_app(
    settings: ControllerSettings,
    *,
    emitter: CompletionEmitter | None = None,
    native_emitter: NativeEmitter | None = None,
) -> FastAPI:
    store = ControllerStore(settings.state_dir, lease_seconds=settings.lease_seconds)
    completion_emitter = emitter or ClawhipCompletionEmitter()
    app = FastAPI(title="Groken Direct Controller", version="1.0.0")
    store_lock = anyio.Lock()

    def require_token(received: str | None, expected: str, detail: str) -> None:
        if received is None or not hmac.compare_digest(received, f"Bearer {expected}"):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail)

    def require_controller(authorization: Annotated[str | None, Header()] = None) -> None:
        require_token(authorization, settings.controller_token, "invalid controller token")

    def require_worker(authorization: Annotated[str | None, Header()] = None) -> None:
        require_token(authorization, settings.worker_token, "invalid worker token")

    def require_enrollment(x_enrollment_token: Annotated[str | None, Header()] = None) -> None:
        if x_enrollment_token is None or not hmac.compare_digest(
            x_enrollment_token, settings.enrollment_token
        ):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid enrollment token")

    async def health() -> dict[str, bool]:
        return {"ok": True}

    async def enroll(_request: EnrollmentRequest) -> EnrollmentResponse:
        return EnrollmentResponse.model_validate(
            {
                "worker_token": settings.worker_token,
                "model_base_url": settings.model_base_url,
                "model_api_key": settings.model_api_key,
                "model": settings.model,
            }
        )

    async def submit(
        request: ControllerJobRequest,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> ControllerJobAccepted:
        workspace = PurePosixPath(request.workspace)
        if workspace.is_absolute() or ".." in workspace.parts or workspace == PurePosixPath("."):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid workspace path")
        if idempotency_key is not None and not 1 <= len(idempotency_key) <= 500:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid idempotency key")
        try:
            async with store_lock:
                job_id = str(uuid.uuid4())
                record = (
                    store.create_idempotent(job_id, idempotency_key, request)
                    if idempotency_key is not None
                    else store.create(job_id, request)
                )
        except ValueError as error:
            raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from error
        return ControllerJobAccepted(job_id=record.job_id, status=record.status)

    async def lease(request: LeaseRequest) -> Response | LeasedJob:
        async with store_lock:
            leased = store.lease_next(request.worker_id)
        if leased is None:
            return Response(status_code=status.HTTP_204_NO_CONTENT)
        return leased

    async def complete(request: CompletionRequest) -> dict[str, bool]:
        try:
            async with store_lock:
                record = store.complete(request)
                if not record.completion_emitted:
                    await completion_emitter.emit(record)
                    _ = store.mark_completion_emitted(record.job_id)
        except (FileNotFoundError, ValueError) as error:
            raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from error
        return {"accepted": True}

    async def job_status(job_id: str) -> ControllerJobRecord:
        try:
            return store.read(job_id)
        except FileNotFoundError as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown job") from error

    app.add_api_route("/healthz", health, methods=["GET"])
    app.add_api_route(
        "/v1/enroll",
        enroll,
        methods=["POST"],
        response_model=EnrollmentResponse,
        dependencies=[Depends(require_enrollment)],
    )
    app.add_api_route(
        "/v1/jobs",
        submit,
        methods=["POST"],
        status_code=status.HTTP_202_ACCEPTED,
        response_model=ControllerJobAccepted,
        dependencies=[Depends(require_controller)],
    )
    app.add_api_route(
        "/v1/jobs/{job_id}",
        job_status,
        methods=["GET"],
        response_model=ControllerJobRecord,
        dependencies=[Depends(require_controller)],
    )
    app.add_api_route(
        "/v1/worker/lease",
        lease,
        methods=["POST"],
        response_model=LeasedJob,
        dependencies=[Depends(require_worker)],
    )
    app.add_api_route(
        "/v1/worker/complete",
        complete,
        methods=["POST"],
        status_code=status.HTTP_202_ACCEPTED,
        dependencies=[Depends(require_worker)],
    )
    app.include_router(
        create_native_router(
            NativeControllerSettings(
                state_dir=settings.state_dir,
                controller_token=settings.controller_token,
                worker_token=settings.worker_token,
                lease_seconds=settings.lease_seconds,
            ),
            emitter=native_emitter,
        )
    )
    return app
