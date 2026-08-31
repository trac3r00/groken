import json
import os
import subprocess
from pathlib import Path
from typing import cast, final

import anyio

from .private_files import write_private_text
from .worker_models import JobExecution, JobRequest
from .worker_store import SecretStore


class WorkerProtocolError(RuntimeError):
    pass


def resolve_workspace(root: Path, requested: str) -> Path:
    if not requested or Path(requested).is_absolute():
        raise ValueError("workspace must be a non-empty relative path")
    resolved_root = root.resolve()
    resolved = (resolved_root / requested).resolve()
    if resolved == resolved_root or resolved_root not in resolved.parents:
        raise ValueError("workspace escapes the configured root")
    return resolved


def extract_final_text(stream: str) -> str:
    final_event: dict[str, object] | None = None
    for line in stream.splitlines():
        try:
            decoded = cast("object", json.loads(line))
        except json.JSONDecodeError:
            continue
        if isinstance(decoded, dict):
            event = cast("dict[str, object]", decoded)
            if event.get("type") == "agent_end":
                final_event = event
    if final_event is None:
        raise WorkerProtocolError("OMO output did not contain agent_end")
    messages = final_event.get("messages")
    if not isinstance(messages, list):
        raise WorkerProtocolError("agent_end did not contain messages")
    for message_value in reversed(cast("list[object]", messages)):
        if not isinstance(message_value, dict):
            continue
        message = cast("dict[str, object]", message_value)
        if message.get("role") != "assistant":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            texts: list[str] = []
            for part_value in cast("list[object]", content):
                if not isinstance(part_value, dict):
                    continue
                part = cast("dict[str, object]", part_value)
                text = part.get("text")
                if part.get("type") == "text" and isinstance(text, str):
                    texts.append(text)
            if texts:
                return "\n".join(texts)
    raise WorkerProtocolError("agent_end had no assistant text")


@final
class OmoRunner:
    def __init__(
        self,
        *,
        omo_command: str,
        state_dir: Path,
        secret_store: SecretStore,
        timeout_seconds: float = 1800,
    ) -> None:
        self._omo_command: str = omo_command
        self._state_dir: Path = state_dir
        self._secret_store: SecretStore = secret_store
        self._timeout_seconds: float = timeout_seconds

    async def run(
        self, job_id: str, request: JobRequest, workspace: Path
    ) -> JobExecution:
        workspace.mkdir(parents=True, exist_ok=True)
        secrets = self._secret_store.load()
        session_dir = self._state_dir / "sessions"
        session_dir.mkdir(parents=True, exist_ok=True)
        command = [
            self._omo_command,
            "--mode",
            "json",
            "--print",
            "--model",
            secrets.model,
            "--session-id",
            job_id,
            "--session-dir",
            str(session_dir),
            "--permission-preset",
            "workspace",
            "--no-approve",
            "--no-context-files",
            request.task,
        ]
        environment = dict(os.environ)
        environment["OMO_CODING_AGENT_DIR"] = str(self._state_dir / "omo")
        try:
            with anyio.fail_after(self._timeout_seconds):
                process = await anyio.run_process(
                    command,
                    cwd=workspace,
                    env=environment,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                )
        except TimeoutError as error:
            raise WorkerProtocolError("OMO execution timed out") from error
        stdout = process.stdout.decode("utf-8", "replace") if process.stdout else ""
        stderr = process.stderr.decode("utf-8", "replace") if process.stderr else ""
        self.write_log(job_id, stdout, stderr)
        if process.returncode != 0:
            raise WorkerProtocolError(
                f"OMO exited {process.returncode}: {stderr[-2000:]}"
            )
        return JobExecution(
            result=extract_final_text(stdout), exit_code=process.returncode
        )

    def write_log(self, job_id: str, stdout: str, stderr: str) -> None:
        logs_dir = self._state_dir / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        path = logs_dir / f"{job_id}.json"
        write_private_text(
            path,
            json.dumps({"stdout": stdout, "stderr": stderr}),
        )
