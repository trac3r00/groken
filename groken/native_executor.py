import base64
import hashlib
import os
import re
import signal
import subprocess
import uuid
from pathlib import Path
from typing import final

import anyio
from anyio import to_thread

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
        try:
            with anyio.fail_after(operation.timeout_ms / 1000):
                process = await anyio.run_process(
                    operation.argv,
                    input=stdin,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    cwd=cwd,
                    env=environment,
                    check=False,
                    start_new_session=True,
                )
        except TimeoutError:
            return {
                "type": operation.type,
                "exit_code": None,
                "stdout_b64": "",
                "stderr_b64": "",
                "timed_out": True,
                "truncated": False,
            }
        stdout = process.stdout or b""
        stderr = process.stderr or b""
        truncated = len(stdout) > MAX_OUTPUT_BYTES or len(stderr) > MAX_OUTPUT_BYTES
        return {
            "type": operation.type,
            "exit_code": process.returncode,
            "stdout_b64": base64.b64encode(stdout[:MAX_OUTPUT_BYTES]).decode(),
            "stderr_b64": base64.b64encode(stderr[:MAX_OUTPUT_BYTES]).decode(),
            "timed_out": False,
            "truncated": truncated,
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
