import base64
import os
import uuid
from typing import assert_never, final

import httpx
from pydantic import ValidationError

from .native_models import (
    NativeAccepted,
    NativeOperationRecord,
    NativeOperationRequest,
    NativeStatus,
    TerminalExec,
)
from .native_wait_models import (
    NativeClientConfigurationError,
    NativeCommandResult,
    NativeResultError,
    NativeTerminalExecResult,
    NativeTerminalFailureError,
    NativeTerminalOperationRecord,
    NativeWaitTimeout,
    NativeWaitTimeoutError,
)


@final
class NativeControllerClient:
    def __init__(
        self,
        *,
        base_url: str | None = None,
        token: str | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        resolved_url = base_url or os.environ.get(
            "GROKEN_CONTROLLER_URL", "http://127.0.0.1:18766"
        )
        resolved_token = token or os.environ.get("GROKEN_CONTROLLER_TOKEN", "")
        if not resolved_token:
            raise NativeClientConfigurationError("GROKEN_CONTROLLER_TOKEN is required")
        self._token = resolved_token
        self._client = client or httpx.Client(base_url=resolved_url, timeout=60)
        self._owns_client = client is None

    def submit(
        self,
        request: NativeOperationRequest,
        *,
        idempotency_key: str | None = None,
    ) -> NativeAccepted:
        response = self._client.post(
            "/v2/operations",
            headers={
                "authorization": f"Bearer {self._token}",
                "idempotency-key": idempotency_key or str(uuid.uuid4()),
            },
            json=request.model_dump(mode="json"),
        )
        _ = response.raise_for_status()
        return NativeAccepted.model_validate(response.json())

    def get(self, operation_id: str) -> NativeOperationRecord:
        response = self._client.get(
            f"/v2/operations/{operation_id}",
            headers={"authorization": f"Bearer {self._token}"},
        )
        _ = response.raise_for_status()
        return NativeOperationRecord.model_validate(response.json())

    def wait(
        self,
        operation_id: str,
        timeout_s: float,
    ) -> NativeTerminalOperationRecord:
        response = self._client.get(
            f"/v2/operations/{operation_id}/wait",
            headers={"authorization": f"Bearer {self._token}"},
            params={"timeout_s": timeout_s},
            timeout=timeout_s + 5,
        )
        if response.status_code == httpx.codes.REQUEST_TIMEOUT:
            timeout = NativeWaitTimeout.model_validate(response.json())
            if timeout.operation_id != operation_id:
                raise NativeResultError(
                    operation_id, "a timeout for a different operation"
                )
            raise NativeWaitTimeoutError(operation_id, timeout.timeout_s)
        _ = response.raise_for_status()
        terminal = NativeTerminalOperationRecord.model_validate(response.json())
        if terminal.operation_id != operation_id:
            raise NativeResultError(operation_id, "a result for a different operation")
        return terminal

    def execute_wait(
        self,
        request: NativeOperationRequest,
        idempotency_key: str,
        timeout_s: float,
    ) -> NativeCommandResult:
        operation = TerminalExec.model_validate(request.operation.model_dump())
        accepted = self.submit(request, idempotency_key=idempotency_key)
        terminal = self.wait(accepted.operation_id, timeout_s)
        if (
            terminal.target != request.target
            or terminal.workspace != request.workspace
            or terminal.operation != request.operation
        ):
            raise NativeResultError(
                accepted.operation_id, "mismatched operation identity"
            )
        match terminal.status:
            case NativeStatus.COMPLETED:
                pass
            case NativeStatus.FAILED:
                raise NativeTerminalFailureError(
                    accepted.operation_id,
                    terminal.error or "worker reported failure",
                )
            case _ as unreachable:
                assert_never(unreachable)
        try:
            result = NativeTerminalExecResult.model_validate(terminal.result)
        except ValidationError as error:
            raise NativeResultError(
                accepted.operation_id,
                "a malformed terminal.exec result",
            ) from error
        exit_code = result.exit_code
        signal = result.signal
        if signal is None and exit_code is not None and exit_code < 0:
            signal = -exit_code
            exit_code = None
        return NativeCommandResult(
            argv=tuple(operation.argv),
            exit_code=exit_code,
            stdout=base64.b64decode(result.stdout_b64, validate=True),
            stderr=base64.b64decode(result.stderr_b64, validate=True),
            timed_out=result.timed_out,
            truncated=result.truncated,
            signal=signal,
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()
