from __future__ import annotations

import base64
import hashlib
import os
import uuid
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Final, final

import httpx
from pydantic import ValidationError

from .env_collectors import (
    CommandRequest,
    CommandResult,
    Inventory,
    NativeAdapterError,
    NativePlaneUnavailable,
    collect_environment,
)
from .env_persistence import ManifestTree, TreeFile
from .env_restore_manifest import LoadedInventory
from .env_restore_run import RestorePendingError, RestoreRunRequest, RestoreRunResult
from .native_client import NativeControllerClient
from .native_models import NativeOperationRequest, TerminalExec
from .native_wait_models import (
    NativeResultError,
    NativeTerminalFailureError,
    NativeWaitTimeoutError,
)


class CapturePhase(StrEnum):
    PLAN = "plan"
    PRE_RESTORE = "pre-restore"
    POST_RESTORE = "post-restore"
    TASK4 = "task4"


@dataclass(frozen=True, slots=True)
class NativeEnvironmentSettings:
    target: str = "groken-box"
    workspace: str = "native"
    wait_grace_s: float = 5.0


_TERMINAL_FAILURES: Final = (NativeTerminalFailureError, NativeResultError)
_CAPTURE_UNAVAILABLE: Final = (
    httpx.NetworkError,
    httpx.TimeoutException,
    NativeWaitTimeoutError,
    NativeTerminalFailureError,
)
_CAPTURE_ADAPTER_ERRORS: Final = (NativeResultError, ValidationError)
_UNAVAILABLE_STATUS_CODES: Final = frozenset(
    {401, 403, 404, 408, 429, 502, 503, 504}
)


def _key(namespace: str, index: int, request: CommandRequest) -> str:
    digest = hashlib.sha256()
    for value in (
        namespace.encode(),
        str(index).encode(),
        *[argument.encode() for argument in request.argv],
        request.stdin,
        str(request.timeout_ms).encode(),
    ):
        digest.update(len(value).to_bytes(8, "big"))
        digest.update(value)
    return f"groken-env-native:{digest.hexdigest()}"


def _native_request(
    settings: NativeEnvironmentSettings,
    request: CommandRequest,
) -> NativeOperationRequest:
    return NativeOperationRequest(
        target=settings.target,
        workspace=settings.workspace,
        operation=TerminalExec(
            type="terminal.exec",
            argv=list(request.argv),
            cwd=".",
            stdin_b64=base64.b64encode(request.stdin).decode(),
            timeout_ms=request.timeout_ms,
        ),
    )


def _failure_result(
    argv: tuple[str, ...],
    error: NativeTerminalFailureError | NativeResultError,
) -> RestoreRunResult:
    return RestoreRunResult(
        argv,
        None,
        b"",
        str(error).encode(),
        False,
        False,
        None,
    )


@final
class NativeCaptureRunner:
    """Task-4 NativeRunner backed by typed terminal.exec completion waits."""

    def __init__(
        self,
        client: NativeControllerClient,
        settings: NativeEnvironmentSettings,
        namespace: str,
    ) -> None:
        self._client = client
        self._settings = settings
        self._scope = f"{namespace}/{uuid.uuid4().hex}"
        self._index = 0

    def run(self, request: CommandRequest) -> CommandResult:
        self._index += 1
        key = _key(self._scope, self._index, request)
        try:
            result = self._client.execute_wait(
                _native_request(self._settings, request),
                idempotency_key=key,
                timeout_s=min(
                    1_800.0, request.timeout_ms / 1_000 + self._settings.wait_grace_s
                ),
            )
        except _CAPTURE_UNAVAILABLE as exc:
            raise NativePlaneUnavailable(str(exc)) from exc
        except _CAPTURE_ADAPTER_ERRORS as exc:
            raise NativeAdapterError(str(exc)) from exc
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in _UNAVAILABLE_STATUS_CODES:
                raise NativePlaneUnavailable(
                    f"native controller unavailable: {exc}"
                ) from exc
            raise NativeAdapterError(str(exc)) from exc
        return CommandResult(
            result.argv,
            result.exit_code,
            result.stdout,
            result.stderr,
            result.timed_out,
            result.truncated,
        )

    def publish(self, tree: ManifestTree) -> None:
        root = PurePosixPath("groken-env") / "manifests" / tree.manifest_id
        for item in tree.files:
            relative = PurePosixPath(item.path)
            if relative.is_absolute() or ".." in relative.parts:
                raise NativeAdapterError("manifest publish path is unsafe")
            destination = root / relative
            commands = (
                CommandRequest(
                    (
                        "/usr/bin/env",
                        "mkdir",
                        "-m",
                        "700",
                        "-p",
                        str(destination.parent),
                    )
                ),
                CommandRequest(("/usr/bin/env", "tee", str(destination)), item.content),
                CommandRequest(("/usr/bin/env", "chmod", "600", str(destination))),
            )
            for command in commands:
                result = self.run(command)
                if result.exit_code != 0 or result.timed_out:
                    raise NativeAdapterError(
                        result.stderr.decode(errors="replace")
                        or "native manifest publish failed"
                    )


@final
class NativeEnvironmentRunner:
    """Production restore runner and deterministic native recapture source."""

    def __init__(
        self,
        client: NativeControllerClient,
        settings: NativeEnvironmentSettings | None = None,
    ) -> None:
        self._client = client
        self._settings = settings or NativeEnvironmentSettings()

    def run_restore(self, request: RestoreRunRequest) -> RestoreRunResult:
        command = CommandRequest(request.argv, request.stdin, request.timeout_ms)
        try:
            result = self._client.execute_wait(
                _native_request(self._settings, command),
                idempotency_key=request.idempotency_key,
                timeout_s=min(
                    1_800.0, request.timeout_ms / 1_000 + self._settings.wait_grace_s
                ),
            )
        except NativeWaitTimeoutError as exc:
            raise RestorePendingError(
                "native restore pending after wait timeout; rerun to resume the same attempt"
            ) from exc
        except ValidationError:
            malformed = NativeResultError(
                request.operation_key,
                "a malformed terminal wait/result envelope",
            )
            return _failure_result(request.argv, malformed)
        except _TERMINAL_FAILURES as exc:
            return _failure_result(request.argv, exc)
        return RestoreRunResult(
            result.argv,
            result.exit_code,
            result.stdout,
            result.stderr,
            result.timed_out,
            result.truncated,
            result.signal,
        )

    def capture(self, manifest_id: str, phase: CapturePhase) -> Inventory:
        namespace = f"restore-capture/{manifest_id}/{phase.value}"
        runner = NativeCaptureRunner(self._client, self._settings, namespace)
        return collect_environment(runner).inventory

    def brewfile_path(self, loaded: LoadedInventory) -> Path | None:
        if loaded.brewfile_path is None:
            return None
        relative = loaded.brewfile_path.relative_to(loaded.path)
        return Path("groken-env") / "manifests" / loaded.manifest_id / relative

    def prepare(self, loaded: LoadedInventory) -> None:
        if loaded.brewfile_path is None:
            return
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(loaded.brewfile_path, flags)
        with os.fdopen(descriptor, "rb") as stream:
            content = stream.read()
        relative = loaded.brewfile_path.relative_to(loaded.path)
        tree = ManifestTree(
            loaded.manifest_id,
            (TreeFile(str(relative), content),),
        )
        runner = NativeCaptureRunner(
            self._client,
            self._settings,
            f"restore-prepare/{loaded.manifest_id}",
        )
        runner.publish(tree)

    def task4_runner(self, namespace: str) -> NativeCaptureRunner:
        return NativeCaptureRunner(
            self._client,
            self._settings,
            f"task4/{namespace}/{CapturePhase.TASK4.value}",
        )

    def close(self) -> None:
        self._client.close()
