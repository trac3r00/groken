import os
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, final

import anyio
import httpx

from .native_executor import NativeExecutor
from .native_models import (
    NativeCompletionRequest,
    NativeLeasedOperation,
    NativeLeaseRequest,
    NativeOperation,
    NativeStatus,
)
from .poller import PollerConfig
from .worker_runner import resolve_workspace


@dataclass(frozen=True)
class NativePollerSettings:
    controller_url: str
    worker_id: str
    state_dir: Path
    workspace_root: Path
    poll_interval_seconds: float = 2


class NativeEnrollmentPending(RuntimeError):
    pass


class Executor(Protocol):
    async def execute(
        self, operation: NativeOperation, workspace: Path
    ) -> dict[str, object]: ...


@final
class NativePoller:
    def __init__(
        self,
        settings: NativePollerSettings,
        *,
        executor: Executor | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings
        self._config_file = settings.state_dir / "poller.json"
        self._pending_file = settings.state_dir / "pending-native-completion.json"
        self._executor = executor or NativeExecutor()
        self._client = client or httpx.AsyncClient(
            base_url=settings.controller_url,
            timeout=60,
        )
        self._owns_client = client is None

    async def run_once(self) -> bool:
        config = self._load_config()
        headers = {"authorization": f"Bearer {config.worker_token}"}
        if await self._flush_pending(headers):
            return True
        response = await self._client.post(
            "/v2/worker/lease",
            headers=headers,
            json=NativeLeaseRequest(
                worker_id=self._settings.worker_id,
                capabilities=["terminal", "files", "processes"],
            ).model_dump(mode="json"),
        )
        if response.status_code == 204:
            return False
        _ = response.raise_for_status()
        leased = NativeLeasedOperation.model_validate(response.json())
        try:
            workspace = resolve_workspace(
                self._settings.workspace_root, leased.workspace
            )
            result = await self._executor.execute(leased.operation, workspace)
            completion = NativeCompletionRequest(
                worker_id=self._settings.worker_id,
                operation_id=leased.operation_id,
                lease_id=leased.lease_id,
                status=NativeStatus.COMPLETED,
                result=result,
            )
        except Exception as error:  # noqa: BLE001 - native operation boundary
            completion = NativeCompletionRequest(
                worker_id=self._settings.worker_id,
                operation_id=leased.operation_id,
                lease_id=leased.lease_id,
                status=NativeStatus.FAILED,
                error=str(error),
            )
        self._write_private(self._pending_file, completion.model_dump_json(indent=2))
        _ = await self._flush_pending(headers)
        return True

    async def run_forever(self) -> None:
        backoff = max(1.0, self._settings.poll_interval_seconds)
        try:
            while True:
                try:
                    worked = await self.run_once()
                except NativeEnrollmentPending:
                    await anyio.sleep(self._settings.poll_interval_seconds)
                    continue
                except httpx.HTTPStatusError as error:
                    if error.response.status_code not in {429, 500, 502, 503, 504}:
                        raise
                    await anyio.sleep(backoff)
                    backoff = min(backoff * 2, 30.0)
                    continue
                except httpx.TransportError:
                    await anyio.sleep(backoff)
                    backoff = min(backoff * 2, 30.0)
                    continue
                backoff = max(1.0, self._settings.poll_interval_seconds)
                if not worked:
                    await anyio.sleep(self._settings.poll_interval_seconds)
        finally:
            if self._owns_client:
                await self._client.aclose()

    def _load_config(self) -> PollerConfig:
        if not self._config_file.is_file():
            raise NativeEnrollmentPending(
                "native poller is waiting for worker enrollment"
            )
        config = PollerConfig.model_validate_json(self._config_file.read_text())
        if config.controller_url.rstrip("/") != self._settings.controller_url.rstrip(
            "/"
        ):
            raise ValueError("controller URL differs from enrolled controller URL")
        return config

    async def _flush_pending(self, headers: dict[str, str]) -> bool:
        if not self._pending_file.is_file():
            return False
        completion = NativeCompletionRequest.model_validate_json(
            self._pending_file.read_text()
        )
        response = await self._client.post(
            "/v2/worker/complete",
            headers=headers,
            json=completion.model_dump(mode="json"),
        )
        if response.status_code == 409:
            rejected = self._settings.state_dir / (
                f"rejected-native-completion-{completion.operation_id}.json"
            )
            os.replace(self._pending_file, rejected)
            return True
        _ = response.raise_for_status()
        self._pending_file.unlink()
        return True

    @staticmethod
    def _write_private(path: Path, content: str) -> None:
        temporary = path.with_suffix(".tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            _ = stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
