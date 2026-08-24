import os
import uuid
from typing import final

import httpx

from .native_models import NativeAccepted, NativeOperationRecord, NativeOperationRequest


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
            raise ValueError("GROKEN_CONTROLLER_TOKEN is required")
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

    def close(self) -> None:
        if self._owns_client:
            self._client.close()
