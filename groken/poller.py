import os
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Protocol, final

import anyio
import httpx
from pydantic import BaseModel, ConfigDict, SecretStr

from .controller_models import (
    CompletionRequest,
    ControllerJobStatus,
    EnrollmentRequest,
    EnrollmentResponse,
    LeasedJob,
    LeaseRequest,
)
from .worker_models import JobExecution, JobRequest
from .worker_runner import OmoRunner, resolve_workspace
from .worker_store import SecretStore, WorkerSecrets


@dataclass(frozen=True)
class PollerSettings:
    controller_url: str
    enrollment_token: str
    worker_id: str
    state_dir: Path
    workspace_root: Path
    omo_command: str
    poll_interval_seconds: float = 2


class PollerConfig(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")
    controller_url: str
    worker_token: str


class Runner(Protocol):
    async def run(self, job_id: str, request: JobRequest, workspace: Path) -> JobExecution: ...


@final
class RemotePoller:
    def __init__(
        self,
        settings: PollerSettings,
        *,
        runner: Runner | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings: PollerSettings = settings
        self._secret_store = SecretStore(settings.state_dir)
        self._config_file: Path = settings.state_dir / "poller.json"
        self._pending_completion_file: Path = settings.state_dir / "pending-completion.json"
        self._client = client or httpx.AsyncClient(base_url=settings.controller_url, timeout=60)
        self._owns_client: bool = client is None
        self._runner = runner or OmoRunner(
            omo_command=settings.omo_command,
            state_dir=settings.state_dir,
            secret_store=self._secret_store,
        )

    async def run_once(self) -> bool:
        config = await self._ensure_enrolled()
        headers = {"authorization": f"Bearer {config.worker_token}"}
        if await self._flush_pending_completion(headers):
            return True
        lease_response = await self._client.post(
            "/v1/worker/lease",
            headers=headers,
            json=LeaseRequest(worker_id=self._settings.worker_id).model_dump(mode="json"),
        )
        if lease_response.status_code == 204:
            return False
        _ = lease_response.raise_for_status()
        leased = LeasedJob.model_validate(lease_response.json())
        request = JobRequest(
            task=leased.task,
            workspace=leased.workspace,
            origin_session_id=leased.origin_session_id,
        )
        try:
            workspace = resolve_workspace(self._settings.workspace_root, leased.workspace)
            execution = await self._runner.run(leased.job_id, request, workspace)
            completion = CompletionRequest(
                worker_id=self._settings.worker_id,
                job_id=leased.job_id,
                lease_id=leased.lease_id,
                status=ControllerJobStatus.COMPLETED,
                result=execution.result,
            )
        except Exception as error:  # noqa: BLE001 - remote job boundary serializes failures
            completion = CompletionRequest(
                worker_id=self._settings.worker_id,
                job_id=leased.job_id,
                lease_id=leased.lease_id,
                status=ControllerJobStatus.FAILED,
                error=str(error),
            )
        self._write_private(
            self._pending_completion_file,
            completion.model_dump_json(indent=2),
        )
        _ = await self._flush_pending_completion(headers)
        return True

    async def run_forever(self) -> None:
        backoff_seconds = max(1.0, self._settings.poll_interval_seconds)
        try:
            while True:
                try:
                    worked = await self.run_once()
                except httpx.HTTPStatusError as error:
                    if error.response.status_code not in {429, 500, 502, 503, 504}:
                        raise
                    await anyio.sleep(backoff_seconds)
                    backoff_seconds = min(backoff_seconds * 2, 30.0)
                    continue
                except httpx.TransportError:
                    await anyio.sleep(backoff_seconds)
                    backoff_seconds = min(backoff_seconds * 2, 30.0)
                    continue
                backoff_seconds = max(1.0, self._settings.poll_interval_seconds)
                if not worked:
                    await anyio.sleep(self._settings.poll_interval_seconds)
        finally:
            if self._owns_client:
                await self._client.aclose()

    async def _ensure_enrolled(self) -> PollerConfig:
        if self._config_file.is_file():
            if not self._secret_store.configured():
                raise ValueError("poller enrollment is incomplete")
            config = PollerConfig.model_validate_json(self._config_file.read_text())
            if config.controller_url.rstrip("/") != self._settings.controller_url.rstrip("/"):
                raise ValueError("controller URL differs from enrolled controller URL")
            return config
        if self._secret_store.has_artifacts():
            self._secret_store.clear()
        response = await self._client.post(
            "/v1/enroll",
            headers={"x-enrollment-token": self._settings.enrollment_token},
            json=EnrollmentRequest(worker_id=self._settings.worker_id).model_dump(mode="json"),
        )
        _ = response.raise_for_status()
        enrolled = EnrollmentResponse.model_validate(response.json())
        self._secret_store.save(
            WorkerSecrets(
                model_base_url=enrolled.model_base_url,
                model_api_key=SecretStr(enrolled.model_api_key),
                worker_token=SecretStr(enrolled.worker_token),
                model=enrolled.model,
            )
        )
        config = PollerConfig(
            controller_url=self._settings.controller_url,
            worker_token=enrolled.worker_token,
        )
        self._write_private(self._config_file, config.model_dump_json(indent=2))
        return config

    async def _flush_pending_completion(self, headers: dict[str, str]) -> bool:
        if not self._pending_completion_file.is_file():
            return False
        completion = CompletionRequest.model_validate_json(
            self._pending_completion_file.read_text()
        )
        response = await self._client.post(
            "/v1/worker/complete",
            headers=headers,
            json=completion.model_dump(mode="json"),
        )
        _ = response.raise_for_status()
        self._pending_completion_file.unlink()
        return True

    @staticmethod
    def _write_private(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            _ = stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
