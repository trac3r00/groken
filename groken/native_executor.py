import base64
import hashlib
import os
import re
import signal
import subprocess
import uuid
from pathlib import Path
from typing import Final, final

import anyio
from anyio import to_thread
from anyio.abc import ByteReceiveStream, Process

from .native_models import (
    MAX_OUTPUT_BYTES,
    FileDelete,
    FileGrep,
    FileList,
    FileMove,
    FileRead,
    FileStat,
    FileWrite,
    FileWriteMode,
    NativeOperation,
    ProcessKill,
    ProcessList,
    TerminalExec,
    TerminalShell,
)

_TERMINATION_GRACE_SECONDS: Final = 1.0


@final
class _OutputCapture:
    __slots__: Final = ("data", "truncated")

    data: bytearray
    truncated: bool

    def __init__(self) -> None:
        self.data = bytearray()
        self.truncated = False

    def append(self, chunk: bytes) -> None:
        remaining = MAX_OUTPUT_BYTES - len(self.data)
        if remaining > 0:
            self.data.extend(chunk[:remaining])
        if len(chunk) > remaining:
            self.truncated = True


@final
class _CommandState:
    __slots__: Final = (
        "completed",
        "process",
        "stderr",
        "stdout",
        "terminating",
        "timed_out",
    )

    completed: anyio.Event
    process: Process
    stderr: _OutputCapture
    stdout: _OutputCapture
    terminating: bool
    timed_out: bool

    def __init__(self, process: Process) -> None:
        self.process = process
        self.stdout = _OutputCapture()
        self.stderr = _OutputCapture()
        self.completed = anyio.Event()
        self.terminating = False
        self.timed_out = False

    async def communicate(self, stdin: bytes) -> None:
        with anyio.CancelScope(shield=True):
            try:
                async with anyio.create_task_group() as task_group:
                    if self.process.stdout is not None:
                        _ = task_group.start_soon(
                            self._drain,
                            self.process.stdout,
                            self.stdout,
                        )
                    if self.process.stderr is not None:
                        _ = task_group.start_soon(
                            self._drain,
                            self.process.stderr,
                            self.stderr,
                        )
                    if self.process.stdin is not None:
                        try:
                            await self.process.stdin.send(stdin)
                        except (anyio.BrokenResourceError, anyio.ClosedResourceError):
                            if not self.terminating:
                                raise
                        await self.process.stdin.aclose()
                    _ = await self.process.wait()
            finally:
                self.completed.set()

    async def enforce_timeout(self, timeout_seconds: float) -> None:
        with anyio.move_on_after(timeout_seconds) as timeout_scope:
            await self.completed.wait()
        if not timeout_scope.cancel_called:
            return

        self.timed_out = True
        await self.terminate_process_group()

    async def terminate_process_group(self) -> None:
        self.terminating = True
        self._signal_process_group(signal.SIGTERM)
        with anyio.move_on_after(_TERMINATION_GRACE_SECONDS):
            await self.completed.wait()
        self._signal_process_group(signal.SIGKILL)
        await self.completed.wait()

    def _signal_process_group(self, signal_number: int) -> None:
        try:
            os.killpg(self.process.pid, signal_number)
        except ProcessLookupError:
            return

    @staticmethod
    async def _drain(stream: ByteReceiveStream, capture: _OutputCapture) -> None:
        async with stream:
            async for chunk in stream:
                capture.append(chunk)


@final
class NativeExecutor:
    async def execute(
        self,
        operation: NativeOperation,
        workspace: Path,
    ) -> dict[str, object]:
        workspace.mkdir(parents=True, exist_ok=True)
        if isinstance(operation, TerminalExec):
            return await self._terminal_exec(operation, workspace)
        if isinstance(operation, TerminalShell):
            return await self._terminal_shell(operation, workspace)
        if isinstance(operation, (ProcessList, ProcessKill)):
            return await to_thread.run_sync(self._process_operation, operation)
        return await to_thread.run_sync(self._file_operation, operation, workspace)

    async def _terminal_exec(
        self,
        operation: TerminalExec,
        workspace: Path,
    ) -> dict[str, object]:
        cwd = self._resolve_path(workspace, operation.cwd)
        if not cwd.is_dir():
            raise ValueError("terminal cwd is not a directory")
        stdin = base64.b64decode(operation.stdin_b64)
        environment = dict(os.environ)
        environment.update(operation.env)
        async with await anyio.open_process(
            operation.argv,
            stdin=subprocess.PIPE if stdin else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
            env=environment,
            start_new_session=True,
        ) as process:
            state = _CommandState(process)
            async with anyio.create_task_group() as task_group:
                _ = task_group.start_soon(state.communicate, stdin)
                _ = task_group.start_soon(
                    state.enforce_timeout,
                    operation.timeout_ms / 1000,
                )
                try:
                    await state.completed.wait()
                except anyio.get_cancelled_exc_class():
                    with anyio.CancelScope(shield=True):
                        await state.terminate_process_group()
                    raise

        return {
            "type": operation.type,
            "exit_code": None if state.timed_out else process.returncode,
            "stdout_b64": base64.b64encode(state.stdout.data).decode(),
            "stderr_b64": base64.b64encode(state.stderr.data).decode(),
            "timed_out": state.timed_out,
            "truncated": state.stdout.truncated or state.stderr.truncated,
        }

    async def _terminal_shell(
        self,
        operation: TerminalShell,
        workspace: Path,
    ) -> dict[str, object]:
        executable = "/bin/bash" if operation.interpreter == "bash" else "/bin/sh"
        result = await self._terminal_exec(
            TerminalExec(
                type="terminal.exec",
                argv=[executable, "-lc", operation.script],
                cwd=operation.cwd,
                stdin_b64=operation.stdin_b64,
                env=operation.env,
                timeout_ms=operation.timeout_ms,
            ),
            workspace,
        )
        result["type"] = operation.type
        return result

    def _file_operation(
        self,
        operation: FileRead
        | FileWrite
        | FileDelete
        | FileMove
        | FileGrep
        | FileStat
        | FileList,
        workspace: Path,
    ) -> dict[str, object]:
        if isinstance(operation, FileRead):
            return self._file_read(operation, workspace)
        if isinstance(operation, FileWrite):
            return self._file_write(operation, workspace)
        if isinstance(operation, FileDelete):
            return self._file_delete(operation, workspace)
        if isinstance(operation, FileMove):
            return self._file_move(operation, workspace)
        if isinstance(operation, FileGrep):
            return self._file_grep(operation, workspace)
        if isinstance(operation, FileStat):
            return self._file_stat(operation, workspace)
        return self._file_list(operation, workspace)

    def _file_read(self, operation: FileRead, workspace: Path) -> dict[str, object]:
        path = self._resolve_path(workspace, operation.path)
        if not path.is_file():
            raise ValueError("file does not exist")
        total_size = path.stat().st_size
        digest = self._sha256(path)
        with path.open("rb") as stream:
            _ = stream.seek(operation.offset)
            content = stream.read(operation.limit)
        return {
            "type": operation.type,
            "content_b64": base64.b64encode(content).decode(),
            "returned_bytes": len(content),
            "total_size": total_size,
            "eof": operation.offset + len(content) >= total_size,
            "sha256": digest,
        }

    def _file_write(self, operation: FileWrite, workspace: Path) -> dict[str, object]:
        path = self._resolve_path(workspace, operation.path)
        parent = self._resolve_path(workspace, str(Path(operation.path).parent))
        if operation.create_parents:
            parent.mkdir(parents=True, exist_ok=True)
        if not parent.is_dir():
            raise ValueError("parent directory does not exist")
        exists = path.exists()
        if operation.mode is FileWriteMode.CREATE and exists:
            raise FileExistsError(path)
        if operation.mode is FileWriteMode.REPLACE and not exists:
            raise FileNotFoundError(path)
        if operation.expected_sha256 is not None and (
            not exists or self._sha256(path) != operation.expected_sha256
        ):
            raise ValueError("file sha256 precondition failed")
        content = base64.b64decode(operation.content_b64)
        temporary = parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                _ = stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            directory = os.open(parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)
        return {
            "type": operation.type,
            "path": operation.path,
            "bytes_written": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }

    def _file_delete(self, operation: FileDelete, workspace: Path) -> dict[str, object]:
        path = self._resolve_path(workspace, operation.path)
        if not path.exists():
            if operation.missing_ok:
                return {"type": operation.type, "path": operation.path, "deleted": False}
            raise FileNotFoundError(path)
        if not path.is_file():
            raise ValueError("file.delete only removes regular files")
        if (
            operation.expected_sha256 is not None
            and self._sha256(path) != operation.expected_sha256
        ):
            raise ValueError("file sha256 precondition failed")
        path.unlink()
        self._fsync_directory(path.parent)
        return {"type": operation.type, "path": operation.path, "deleted": True}

    def _file_move(self, operation: FileMove, workspace: Path) -> dict[str, object]:
        source = self._resolve_path(workspace, operation.source)
        destination = self._resolve_path(workspace, operation.destination)
        if not source.is_file():
            raise FileNotFoundError(source)
        if destination.exists() and not operation.replace:
            raise FileExistsError(destination)
        if not destination.parent.is_dir():
            raise ValueError("destination parent does not exist")
        digest = self._sha256(source)
        if (
            operation.expected_source_sha256 is not None
            and digest != operation.expected_source_sha256
        ):
            raise ValueError("source sha256 precondition failed")
        if operation.replace:
            os.replace(source, destination)
        else:
            _ = source.rename(destination)
        self._fsync_directory(source.parent)
        if destination.parent != source.parent:
            self._fsync_directory(destination.parent)
        return {
            "type": operation.type,
            "source": operation.source,
            "destination": operation.destination,
            "sha256": digest,
        }

    def _file_grep(self, operation: FileGrep, workspace: Path) -> dict[str, object]:
        path = self._resolve_path(workspace, operation.path)
        if not path.is_file():
            raise FileNotFoundError(path)
        pattern = re.compile(operation.pattern)
        matches: list[dict[str, object]] = []
        truncated = False
        with path.open(encoding="utf-8", errors="strict") as stream:
            for line_number, line in enumerate(stream, start=1):
                if pattern.search(line) is None:
                    continue
                if len(matches) >= operation.max_matches:
                    truncated = True
                    break
                matches.append({"line_number": line_number, "line": line.rstrip("\n")})
        return {
            "type": operation.type,
            "path": operation.path,
            "matches": matches,
            "truncated": truncated,
        }

    def _file_stat(self, operation: FileStat, workspace: Path) -> dict[str, object]:
        path = self._resolve_path(workspace, operation.path)
        stat = path.stat()
        return {
            "type": operation.type,
            "path": operation.path,
            "size": stat.st_size,
            "mode": stat.st_mode & 0o7777,
            "is_file": path.is_file(),
            "is_dir": path.is_dir(),
            "sha256": self._sha256(path) if path.is_file() else None,
        }

    def _file_list(self, operation: FileList, workspace: Path) -> dict[str, object]:
        path = self._resolve_path(workspace, operation.path)
        if not path.is_dir():
            raise ValueError("list path is not a directory")
        names = sorted(entry.name for entry in path.iterdir())[: operation.limit]
        return {"type": operation.type, "path": operation.path, "entries": names}

    def _process_operation(
        self,
        operation: ProcessList | ProcessKill,
    ) -> dict[str, object]:
        if isinstance(operation, ProcessKill):
            signal_value = {
                "TERM": signal.SIGTERM,
                "KILL": signal.SIGKILL,
                "INT": signal.SIGINT,
            }[operation.signal]
            os.kill(operation.pid, signal_value)
            return {
                "type": operation.type,
                "pid": operation.pid,
                "signal": operation.signal,
                "sent": True,
            }
        completed = subprocess.run(
            ["/bin/ps", "-axo", "pid=,ppid=,user=,stat=,command="],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr.strip() or "process listing failed")
        processes: list[dict[str, object]] = []
        for line in completed.stdout.splitlines():
            parts = line.strip().split(maxsplit=4)
            if len(parts) != 5:
                continue
            pid, ppid, user, state, command = parts
            if operation.query is not None and operation.query not in command:
                continue
            processes.append(
                {
                    "pid": int(pid),
                    "ppid": int(ppid),
                    "user": user,
                    "state": state,
                    "command": command,
                }
            )
            if len(processes) >= operation.limit:
                break
        return {"type": operation.type, "processes": processes}

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _resolve_path(workspace: Path, relative: str) -> Path:
        root = workspace.resolve()
        path = (root / relative).resolve(strict=False)
        if path != root and root not in path.parents:
            raise ValueError("path escapes the selected workspace")
        return path

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()
