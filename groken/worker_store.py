import json
import os
from pathlib import Path
from typing import ClassVar, final

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, SecretStr

from .worker_models import JobRecord


class WorkerSecrets(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    model_base_url: AnyHttpUrl
    model_api_key: SecretStr
    worker_token: SecretStr
    model: str


@final
class SecretStore:
    def __init__(self, state_dir: Path) -> None:
        self._state_dir: Path = state_dir
        self.secrets_file: Path = state_dir / "secrets.json"
        self._model_key_file: Path = state_dir / "model-api-key"
        self._models_file: Path = state_dir / "omo" / "models.json"

    def configured(self) -> bool:
        return all(
            path.is_file()
            for path in (self.secrets_file, self._model_key_file, self._models_file)
        )

    def has_artifacts(self) -> bool:
        return any(
            path.exists()
            for path in (self.secrets_file, self._model_key_file, self._models_file)
        )

    def clear(self) -> None:
        for path in (self.secrets_file, self._model_key_file, self._models_file):
            path.unlink(missing_ok=True)

    def load(self) -> WorkerSecrets:
        return WorkerSecrets.model_validate_json(self.secrets_file.read_text())

    def save(self, secrets: WorkerSecrets) -> None:
        if self.configured():
            raise FileExistsError("worker is already configured")
        self._state_dir.mkdir(parents=True, exist_ok=True)
        self._models_file.parent.mkdir(parents=True, exist_ok=True)
        payload = secrets.model_dump(mode="json")
        payload["model_api_key"] = secrets.model_api_key.get_secret_value()
        payload["worker_token"] = secrets.worker_token.get_secret_value()
        self._write_private(self.secrets_file, json.dumps(payload, indent=2))
        self._write_private(self._model_key_file, secrets.model_api_key.get_secret_value())
        models = {
            "providers": {
                "llm-pool": {
                    "baseUrl": str(secrets.model_base_url),
                    "api": "openai-responses",
                    "apiKey": f"!env cat {self._model_key_file}",
                    "authHeader": True,
                    "models": [{"id": secrets.model.removeprefix("llm-pool/")}],
                }
            }
        }
        self._write_private(self._models_file, json.dumps(models, indent=2))

    @staticmethod
    def _write_private(path: Path, content: str) -> None:
        temporary = path.with_suffix(f"{path.suffix}.tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            _ = stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)


@final
class JobStore:
    def __init__(self, state_dir: Path) -> None:
        self._jobs_dir: Path = state_dir / "jobs"
        self._jobs_dir.mkdir(parents=True, exist_ok=True)

    def write(self, record: JobRecord) -> None:
        path = self._path(record.job_id)
        temporary = path.with_suffix(".tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            _ = stream.write(record.model_dump_json(indent=2))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)

    def read(self, job_id: str) -> JobRecord:
        return JobRecord.model_validate_json(self._path(job_id).read_text())

    def _path(self, job_id: str) -> Path:
        return self._jobs_dir / f"{job_id}.json"
