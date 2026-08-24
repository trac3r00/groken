import argparse
import base64
import json
from typing import Literal, cast

from .native_client import NativeControllerClient
from .native_models import (
    FileDelete,
    FileGrep,
    FileList,
    FileMove,
    FileRead,
    FileStat,
    FileWrite,
    FileWriteMode,
    NativeOperationRequest,
    ProcessKill,
    ProcessList,
    TerminalExec,
    TerminalShell,
)


def _request(args: argparse.Namespace) -> NativeOperationRequest:
    command = cast("str", args.command)
    if command == "exec":
        argv = cast("list[str]", args.argv)
        if argv and argv[0] == "--":
            argv = argv[1:]
        operation = TerminalExec(
            type="terminal.exec",
            argv=argv,
            cwd=cast("str", args.cwd),
            stdin_b64=base64.b64encode(cast("str", args.stdin).encode()).decode(),
            timeout_ms=cast("int", args.timeout_ms),
        )
    elif command == "shell":
        operation = TerminalShell(
            type="terminal.shell",
            interpreter=cast("Literal['bash', 'posix-sh']", args.interpreter),
            script=cast("str", args.script),
            cwd=cast("str", args.cwd),
            timeout_ms=cast("int", args.timeout_ms),
        )
    elif command == "file-read":
        operation = FileRead(
            type="file.read",
            path=cast("str", args.path),
            offset=cast("int", args.offset),
            limit=cast("int", args.limit),
        )
    elif command == "file-write":
        operation = FileWrite(
            type="file.write",
            path=cast("str", args.path),
            content_b64=base64.b64encode(cast("str", args.text).encode()).decode(),
            mode=FileWriteMode(cast("str", args.mode)),
            create_parents=cast("bool", args.create_parents),
        )
    elif command == "file-delete":
        operation = FileDelete(
            type="file.delete",
            path=cast("str", args.path),
            expected_sha256=cast("str | None", args.expected_sha256),
            missing_ok=cast("bool", args.missing_ok),
        )
    elif command == "file-move":
        operation = FileMove(
            type="file.move",
            source=cast("str", args.source),
            destination=cast("str", args.destination),
            replace=cast("bool", args.replace),
        )
    elif command == "file-grep":
        operation = FileGrep(
            type="file.grep",
            path=cast("str", args.path),
            pattern=cast("str", args.pattern),
            max_matches=cast("int", args.max_matches),
        )
    elif command == "file-stat":
        operation = FileStat(type="file.stat", path=cast("str", args.path))
    elif command == "file-list":
        operation = FileList(
            type="file.list",
            path=cast("str", args.path),
            limit=cast("int", args.limit),
        )
    elif command == "process-list":
        operation = ProcessList(
            type="process.list",
            query=cast("str | None", args.query),
            limit=cast("int", args.limit),
        )
    elif command == "process-kill":
        operation = ProcessKill(
            type="process.kill",
            pid=cast("int", args.pid),
            signal=cast("Literal['TERM', 'KILL', 'INT']", args.signal),
        )
    else:
        raise ValueError(f"unsupported command: {command}")
    return NativeOperationRequest(
        target=cast("str", args.target),
        workspace=cast("str", args.workspace),
        origin_session_id=cast("str | None", args.origin_session_id),
        operation=operation,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="groken-native")
    _ = parser.add_argument("--controller-url")
    _ = parser.add_argument("--target", default="groken-box")
    _ = parser.add_argument("--workspace", default="native")
    _ = parser.add_argument("--origin-session-id")
    _ = parser.add_argument("--idempotency-key")
    sub = parser.add_subparsers(dest="command", required=True)

    execute = sub.add_parser("exec")
    _ = execute.add_argument("--cwd", default=".")
    _ = execute.add_argument("--stdin", default="")
    _ = execute.add_argument("--timeout-ms", type=int, default=30_000)
    _ = execute.add_argument("argv", nargs=argparse.REMAINDER)

    shell = sub.add_parser("shell")
    _ = shell.add_argument("--interpreter", choices=["bash", "posix-sh"], default="bash")
    _ = shell.add_argument("--cwd", default=".")
    _ = shell.add_argument("--timeout-ms", type=int, default=30_000)
    _ = shell.add_argument("--script", required=True)

    read = sub.add_parser("file-read")
    _ = read.add_argument("path")
    _ = read.add_argument("--offset", type=int, default=0)
    _ = read.add_argument("--limit", type=int, default=1_048_576)

    write = sub.add_parser("file-write")
    _ = write.add_argument("path")
    _ = write.add_argument("--text", required=True)
    _ = write.add_argument(
        "--mode",
        choices=[mode.value for mode in FileWriteMode],
        default="create",
    )
    _ = write.add_argument("--create-parents", action="store_true")

    delete = sub.add_parser("file-delete")
    _ = delete.add_argument("path")
    _ = delete.add_argument("--expected-sha256")
    _ = delete.add_argument("--missing-ok", action="store_true")

    move = sub.add_parser("file-move")
    _ = move.add_argument("source")
    _ = move.add_argument("destination")
    _ = move.add_argument("--replace", action="store_true")

    grep = sub.add_parser("file-grep")
    _ = grep.add_argument("path")
    _ = grep.add_argument("pattern")
    _ = grep.add_argument("--max-matches", type=int, default=1_000)

    stat = sub.add_parser("file-stat")
    _ = stat.add_argument("path")

    listing = sub.add_parser("file-list")
    _ = listing.add_argument("path", nargs="?", default=".")
    _ = listing.add_argument("--limit", type=int, default=1_000)

    process_list = sub.add_parser("process-list")
    _ = process_list.add_argument("--query")
    _ = process_list.add_argument("--limit", type=int, default=1_000)

    process_kill = sub.add_parser("process-kill")
    _ = process_kill.add_argument("pid", type=int)
    _ = process_kill.add_argument("--signal", choices=["TERM", "KILL", "INT"], default="TERM")

    get = sub.add_parser("get")
    _ = get.add_argument("operation_id")
    return parser


def main() -> None:
    args = _parser().parse_args()
    command = cast("str", args.command)
    client = NativeControllerClient(base_url=cast("str | None", args.controller_url))
    try:
        if command == "get":
            record = client.get(cast("str", args.operation_id))
            print(record.model_dump_json(indent=2))
            return
        accepted = client.submit(
            _request(args),
            idempotency_key=cast("str | None", args.idempotency_key),
        )
        print(json.dumps(accepted.model_dump(mode="json"), indent=2))
    finally:
        client.close()


if __name__ == "__main__":
    main()
