import base64
from pathlib import Path
from typing import cast

import anyio
import pytest
from pydantic import ValidationError

from groken.native_executor import NativeExecutor
from groken.native_models import (
    FileDelete,
    FileGrep,
    FileMove,
    FileRead,
    FileWrite,
    FileWriteMode,
    NativeOperationRequest,
    ProcessKill,
    ProcessList,
    TerminalExec,
    TerminalShell,
)


def test_native_models_reject_prompt_relative_exec_and_traversal() -> None:
    with pytest.raises(ValidationError):
        _ = NativeOperationRequest.model_validate(
            {
                "target": "box",
                "workspace": "qa",
                "task": "interpret me",
                "operation": {"type": "terminal.exec", "argv": ["printf"]},
            }
        )
    with pytest.raises(ValidationError, match="absolute executable"):
        _ = TerminalExec(type="terminal.exec", argv=["printf"])
    with pytest.raises(ValidationError, match="selected workspace"):
        _ = FileRead(type="file.read", path="../secret")


@pytest.mark.anyio
async def test_terminal_exec_preserves_literal_argv_without_shell(tmp_path: Path) -> None:
    executor = NativeExecutor()
    operation = TerminalExec(
        type="terminal.exec",
        argv=["/usr/bin/printf", "%s", "; touch escaped"],
    )

    result = await executor.execute(operation, tmp_path)

    assert result["type"] == "terminal.exec"
    assert result["exit_code"] == 0
    assert base64.b64decode(str(result["stdout_b64"])) == b"; touch escaped"
    assert not (tmp_path / "escaped").exists()


@pytest.mark.anyio
async def test_file_write_read_binary_roundtrip(tmp_path: Path) -> None:
    executor = NativeExecutor()
    content = b"\x00native\xff"
    write = FileWrite(
        type="file.write",
        path="nested/proof.bin",
        content_b64=base64.b64encode(content).decode(),
        mode=FileWriteMode.CREATE,
        create_parents=True,
    )

    write_result = await executor.execute(write, tmp_path)
    read_result = await executor.execute(
        FileRead(type="file.read", path="nested/proof.bin"),
        tmp_path,
    )

    assert write_result["sha256"] == read_result["sha256"]
    assert base64.b64decode(str(read_result["content_b64"])) == content


@pytest.mark.anyio
async def test_file_read_rejects_symlink_escape(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-secret"
    _ = outside.write_text("secret")
    (tmp_path / "link").symlink_to(outside)

    with pytest.raises(ValueError, match="workspace"):
        _ = await NativeExecutor().execute(
            FileRead(type="file.read", path="link"),
            tmp_path,
        )


@pytest.mark.anyio
async def test_explicit_shell_is_separate_and_returns_output(tmp_path: Path) -> None:
    result = await NativeExecutor().execute(
        TerminalShell(
            type="terminal.shell",
            interpreter="bash",
            script="printf '%s' shell-native",
        ),
        tmp_path,
    )

    assert result["type"] == "terminal.shell"
    assert result["exit_code"] == 0
    assert base64.b64decode(str(result["stdout_b64"])) == b"shell-native"


@pytest.mark.anyio
async def test_file_move_grep_and_delete(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    _ = source.write_text("alpha\nbeta\nalpha-two\n")
    executor = NativeExecutor()

    moved = await executor.execute(
        FileMove(type="file.move", source="source.txt", destination="moved.txt"),
        tmp_path,
    )
    grep = await executor.execute(
        FileGrep(type="file.grep", path="moved.txt", pattern="^alpha"),
        tmp_path,
    )
    deleted = await executor.execute(
        FileDelete(
            type="file.delete",
            path="moved.txt",
            expected_sha256=str(moved["sha256"]),
        ),
        tmp_path,
    )

    assert grep["matches"] == [
        {"line_number": 1, "line": "alpha"},
        {"line_number": 3, "line": "alpha-two"},
    ]
    assert deleted["deleted"] is True
    assert not (tmp_path / "moved.txt").exists()


@pytest.mark.anyio
async def test_process_list_and_kill(tmp_path: Path) -> None:
    process = await anyio.open_process(["/bin/sleep", "60"])
    assert process.pid is not None
    try:
        executor = NativeExecutor()
        listing = await executor.execute(
            ProcessList(type="process.list", limit=10_000),
            tmp_path,
        )
        killed = await executor.execute(
            ProcessKill(type="process.kill", pid=process.pid, signal="TERM"),
            tmp_path,
        )
        _ = await process.wait()

        processes = cast("list[dict[str, object]]", listing["processes"])
        assert any(entry["pid"] == process.pid for entry in processes)
        assert killed == {
            "type": "process.kill",
            "pid": process.pid,
            "signal": "TERM",
            "sent": True,
        }
    finally:
        if process.returncode is None:
            process.kill()
            _ = await process.wait()
