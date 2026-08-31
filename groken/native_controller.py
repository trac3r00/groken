import hmac
import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Protocol, assert_never, final

import anyio
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response, status
from fastapi.responses import JSONResponse

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
from .native_wait_models import NativeTerminalOperationRecord, NativeWaitTimeout
from .worker_models import JobRecord, JobStatus


@dataclass(frozen=True, slots=True)
class NativeControllerSettings:
    state_dir: Path
    controller_token: str
    worker_token: str
    lease_seconds: int = 3600


class NativeEmitter(Protocol):
    async def emit(self, record: NativeOperationRecord) -> None: ...


@final
class NativeOperationWaiters:
    """Own app-local completion events keyed by durable operation identity."""

    def __init__(self) -> None:
        self._events: dict[str, list[anyio.Event]] = {}

    def subscribe(self, operation_id: str) -> anyio.Event:
        event = anyio.Event()
        self._events.setdefault(operation_id, []).append(event)
        return event

    def notify(self, operation_id: str) -> None:
        for event in self._events.pop(operation_id, []):
            event.set()

    def unsubscribe(self, operation_id: str, event: anyio.Event) -> None:
        events = self._events.get(operation_id)
        if events is None:
            return
        self._events[operation_id] = [
            candidate for candidate in events if candidate is not event
        ]
        if not self._events[operation_id]:
            del self._events[operation_id]


@final
class ClawhipNativeEmitter:
    def __init__(self) -> None:
        self._emitter = ClawhipEmitter()

    async def emit(self, record: NativeOperationRecord) -> None:
        result = (
            json.dumps(record.result, sort_keys=True)
            if record.result is not None
            else None
        )
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
    waiters: NativeOperationWaiters | None = None,
) -> APIRouter:
    router = APIRouter()
    store = NativeStore(
        settings.state_dir,
        lease_seconds=settings.lease_seconds,
    )
    completion_emitter = emitter or ClawhipNativeEmitter()
    operation_waiters = waiters or NativeOperationWaiters()
    lock = anyio.Lock()

    def require_token(received: str | None, expected: str, detail: str) -> None:
        if received is None or not hmac.compare_digest(received, f"Bearer {expected}"):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail)

    def require_controller(
        authorization: Annotated[str | None, Header()] = None,
    ) -> None:
        require_token(
            authorization, settings.controller_token, "invalid controller token"
        )

    def require_worker(authorization: Annotated[str | None, Header()] = None) -> None:
        require_token(authorization, settings.worker_token, "invalid worker token")

    async def submit(
        request: NativeOperationRequest,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> NativeAccepted:
        if idempotency_key is None or not 1 <= len(idempotency_key) <= 500:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, "idempotency key required"
            )
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
                operation_waiters.notify(record.operation_id)
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
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, "unknown operation"
            ) from error

    async def operation_wait(
        operation_id: str,
        timeout_s: Annotated[float, Query(gt=0, le=1_800)],
    ) -> NativeTerminalOperationRecord | JSONResponse:
        event: anyio.Event | None = None
        try:
            async with lock:
                try:
                    record = store.read(operation_id)
                except FileNotFoundError as error:
                    raise HTTPException(
                        status.HTTP_404_NOT_FOUND, "unknown operation"
                    ) from error
                match record.status:
                    case NativeStatus.COMPLETED | NativeStatus.FAILED:
                        return NativeTerminalOperationRecord.model_validate(
                            record.model_dump()
                        )
                    case NativeStatus.QUEUED | NativeStatus.LEASED:
                        event = operation_waiters.subscribe(operation_id)
                    case _ as unreachable:
                        assert_never(unreachable)
            with anyio.move_on_after(timeout_s) as timeout_scope:
                await event.wait()
            if timeout_scope.cancel_called:
                timeout = NativeWaitTimeout(
                    operation_id=operation_id, timeout_s=timeout_s
                )
                return JSONResponse(
                    status_code=status.HTTP_408_REQUEST_TIMEOUT,
                    content=timeout.model_dump(mode="json"),
                )
            async with lock:
                terminal = store.read(operation_id)
            return NativeTerminalOperationRecord.model_validate(terminal.model_dump())
        finally:
            if event is not None:
                with anyio.CancelScope(shield=True):
                    async with lock:
                        operation_waiters.unsubscribe(operation_id, event)

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
        "/v2/operations/{operation_id}/wait",
        operation_wait,
        methods=["GET"],
        response_model=NativeTerminalOperationRecord,
        responses={status.HTTP_408_REQUEST_TIMEOUT: {"model": NativeWaitTimeout}},
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
