import asyncio
import base64
import json
from typing import Literal, cast

ExecServiceClient = None

from mcp.server.mcpserver import MCPServer

from .native_client import NativeControllerClient
from .native_models import (
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

server = MCPServer("groken-native")


def _submit(request: NativeOperationRequest, idempotency_key: str | None) -> str:
    client = NativeControllerClient()
    try:
        return client.submit(
            request,
            idempotency_key=idempotency_key,
        ).model_dump_json(indent=2)
    finally:
        client.close()


def native_terminal_exec(
    argv: list[str],
    workspace: str,
    cwd: str = ".",
    target: str = "groken-box",
    stdin_text: str = "",
    timeout_ms: int = 30_000,
    idempotency_key: str | None = None,
) -> str:
    """Execute exact argv on the remote computer without a shell or agent interpretation."""
    return _submit(
        NativeOperationRequest(
            target=target,
            workspace=workspace,
            operation=TerminalExec(
                type="terminal.exec",
                argv=argv,
                cwd=cwd,
                stdin_b64=base64.b64encode(stdin_text.encode()).decode(),
                timeout_ms=timeout_ms,
            ),
        ),
        idempotency_key,
    )


def native_terminal_shell(
    script: str,
    workspace: str,
    target: str = "groken-box",
    interpreter: Literal["bash", "posix-sh"] = "bash",
    cwd: str = ".",
    timeout_ms: int = 30_000,
    idempotency_key: str | None = None,
) -> str:
    """Run an explicitly selected shell script remotely without agent interpretation."""
    return _submit(
        NativeOperationRequest(
            target=target,
            workspace=workspace,
            operation=TerminalShell(
                type="terminal.shell",
                interpreter=interpreter,
                script=script,
                cwd=cwd,
                timeout_ms=timeout_ms,
            ),
        ),
        idempotency_key,
    )


def native_file_read(
    path: str,
    workspace: str,
    target: str = "groken-box",
    offset: int = 0,
    limit: int = 1_048_576,
    idempotency_key: str | None = None,
) -> str:
    """Read bounded bytes from a remote workspace file without agent interpretation."""
    return _submit(
        NativeOperationRequest(
            target=target,
            workspace=workspace,
            operation=FileRead(type="file.read", path=path, offset=offset, limit=limit),
        ),
        idempotency_key,
    )


def native_file_write_text(
    path: str,
    text: str,
    workspace: str,
    target: str = "groken-box",
    mode: str = "create",
    create_parents: bool = False,
    idempotency_key: str | None = None,
) -> str:
    """Write UTF-8 text atomically to a remote workspace file without an agent."""
    return _submit(
        NativeOperationRequest(
            target=target,
            workspace=workspace,
            operation=FileWrite(
                type="file.write",
                path=path,
                content_b64=base64.b64encode(text.encode()).decode(),
                mode=FileWriteMode(mode),
                create_parents=create_parents,
            ),
        ),
        idempotency_key,
    )


def native_file_delete(
    path: str,
    workspace: str,
    target: str = "groken-box",
    expected_sha256: str | None = None,
    missing_ok: bool = False,
    idempotency_key: str | None = None,
) -> str:
    """Delete one remote workspace file with optional hash precondition."""
    return _submit(
        NativeOperationRequest(
            target=target,
            workspace=workspace,
            operation=FileDelete(
                type="file.delete",
                path=path,
                expected_sha256=expected_sha256,
                missing_ok=missing_ok,
            ),
        ),
        idempotency_key,
    )


def native_file_move(
    source: str,
    destination: str,
    workspace: str,
    target: str = "groken-box",
    replace: bool = False,
    idempotency_key: str | None = None,
) -> str:
    """Move one remote workspace file without agent interpretation."""
    return _submit(
        NativeOperationRequest(
            target=target,
            workspace=workspace,
            operation=FileMove(
                type="file.move",
                source=source,
                destination=destination,
                replace=replace,
            ),
        ),
        idempotency_key,
    )


def native_file_grep(
    path: str,
    pattern: str,
    workspace: str,
    target: str = "groken-box",
    max_matches: int = 1_000,
    idempotency_key: str | None = None,
) -> str:
    """Search one UTF-8 remote file with a bounded regular expression."""
    return _submit(
        NativeOperationRequest(
            target=target,
            workspace=workspace,
            operation=FileGrep(
                type="file.grep",
                path=path,
                pattern=pattern,
                max_matches=max_matches,
            ),
        ),
        idempotency_key,
    )


def native_process_list(
    target: str = "groken-box",
    query: str | None = None,
    limit: int = 1_000,
    idempotency_key: str | None = None,
) -> str:
    """List bounded remote process metadata directly."""
    return _submit(
        NativeOperationRequest(
            target=target,
            workspace="native-processes",
            operation=ProcessList(type="process.list", query=query, limit=limit),
        ),
        idempotency_key,
    )


def native_process_kill(
    pid: int,
    target: str = "groken-box",
    signal: Literal["TERM", "KILL", "INT"] = "TERM",
    idempotency_key: str | None = None,
) -> str:
    """Send an explicit signal to one remote process ID."""
    return _submit(
        NativeOperationRequest(
            target=target,
            workspace="native-processes",
            operation=ProcessKill(type="process.kill", pid=pid, signal=signal),
        ),
        idempotency_key,
    )


async def direct_cloud_exec(command: str) -> str:
    """Execute a command directly, bypassing the durable queue."""
    global ExecServiceClient
    if ExecServiceClient is None:
        from .exec_service import ExecServiceClient as client_type

        ExecServiceClient = client_type
    client = ExecServiceClient()
    result = await client.execute(command)
    stdout = result.stdout
    stderr = result.stderr
    return json.dumps({"stdout": stdout, "stderr": stderr})


def native_operation_get(operation_id: str) -> str:
    """Read a native operation's durable status and structured result."""
    client = NativeControllerClient()
    try:
        return client.get(operation_id).model_dump_json(indent=2)
    finally:
        client.close()


for function in (
    native_terminal_exec,
    native_terminal_shell,
    native_file_read,
    native_file_write_text,
    native_file_delete,
    native_file_move,
    native_file_grep,
    native_process_list,
    native_process_kill,
    native_operation_get,
    direct_cloud_exec,
):
    server.add_tool(function)


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(prog="groken-native-mcp")
    _ = parser.add_argument("--transport", choices=["stdio", "sse", "http"], default="stdio")
    _ = parser.add_argument("--host", default="127.0.0.1")
    _ = parser.add_argument("--port", type=int, default=8323)
    args = parser.parse_args()
    transport = cast("str", args.transport)
    host = cast("str", args.host)
    port = cast("int", args.port)
    if transport == "stdio":
        asyncio.run(server.run_stdio_async())
    elif transport == "sse":
        asyncio.run(server.run_sse_async(host=host, port=port))
    else:
        asyncio.run(server.run_streamable_http_async(host=host, port=port))


if __name__ == "__main__":
    main()
