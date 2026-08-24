import hmac
import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Protocol, final

import anyio
from fastapi import APIRouter, Depends, Header, HTTPException, Response, status

from .callback_app import ClawhipEmitter
from .native_models import (
    NativeAccepted,
    NativeCompletionRequest,
    NativeLeasedOperation,
    NativeLeaseRequest,
    NativeOperationRecord,
    NativeOperationRequest,
    NativeStatus,
)
from .native_store import NativeStore
from .worker_models import JobRecord, JobStatus


@dataclass(frozen=True)
class NativeControllerSettings:
    state_dir: Path
    controller_token: str
    worker_token: str
    lease_seconds: int = 3600


class NativeEmitter(Protocol):
    async def emit(self, record: NativeOperationRecord) -> None: ...


@final
class ClawhipNativeEmitter:
    def __init__(self) -> None:
        self._emitter = ClawhipEmitter()

    async def emit(self, record: NativeOperationRecord) -> None:
        result = json.dumps(record.result, sort_keys=True) if record.result is not None else None
        await self._emitter.emit(
            JobRecord(
                job_id=record.operation_id,
                status=(
                    JobStatus.COMPLETED
                    if record.status is NativeStatus.COMPLETED
                    else JobStatus.FAILED
                ),
                origin_session_id=record.origin_session_id,
                result=result,
                error=record.error,
            )
        )


def create_native_router(
    settings: NativeControllerSettings,
    *,
    emitter: NativeEmitter | None = None,
) -> APIRouter:
    router = APIRouter()
    store = NativeStore(
        settings.state_dir,
        lease_seconds=settings.lease_seconds,
    )
    completion_emitter = emitter or ClawhipNativeEmitter()
    lock = anyio.Lock()

    def require_token(received: str | None, expected: str, detail: str) -> None:
        if received is None or not hmac.compare_digest(received, f"Bearer {expected}"):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail)

    def require_controller(authorization: Annotated[str | None, Header()] = None) -> None:
        require_token(authorization, settings.controller_token, "invalid controller token")

    def require_worker(authorization: Annotated[str | None, Header()] = None) -> None:
        require_token(authorization, settings.worker_token, "invalid worker token")

    async def submit(
        request: NativeOperationRequest,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> NativeAccepted:
        if idempotency_key is None or not 1 <= len(idempotency_key) <= 500:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "idempotency key required")
        try:
            async with lock:
                record = store.create_idempotent(
                    str(uuid.uuid4()),
                    idempotency_key,
                    request,
                )
        except ValueError as error:
            raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from error
        return NativeAccepted(operation_id=record.operation_id, status=record.status)

    async def lease(request: NativeLeaseRequest) -> Response | NativeLeasedOperation:
        async with lock:
            leased = store.lease_next(request.worker_id, set(request.capabilities))
        if leased is None:
            return Response(status_code=status.HTTP_204_NO_CONTENT)
        return leased

    async def complete(request: NativeCompletionRequest) -> dict[str, bool]:
        try:
            async with lock:
                record = store.complete(request)
                if not record.completion_emitted:
                    await completion_emitter.emit(record)
                    _ = store.mark_emitted(record.operation_id)
        except (FileNotFoundError, ValueError) as error:
            raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from error
        return {"accepted": True}

    async def operation_status(operation_id: str) -> NativeOperationRecord:
        try:
            return store.read(operation_id)
        except FileNotFoundError as error:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown operation") from error

    router.add_api_route(
        "/v2/operations",
        submit,
        methods=["POST"],
        status_code=status.HTTP_202_ACCEPTED,
        response_model=NativeAccepted,
        dependencies=[Depends(require_controller)],
    )
    router.add_api_route(
        "/v2/operations/{operation_id}",
        operation_status,
        methods=["GET"],
        response_model=NativeOperationRecord,
        dependencies=[Depends(require_controller)],
    )
    router.add_api_route(
        "/v2/worker/lease",
        lease,
        methods=["POST"],
        response_model=NativeLeasedOperation,
        dependencies=[Depends(require_worker)],
    )
    router.add_api_route(
        "/v2/worker/complete",
        complete,
        methods=["POST"],
        status_code=status.HTTP_202_ACCEPTED,
        dependencies=[Depends(require_worker)],
    )
    return router
