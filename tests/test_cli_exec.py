import sys
from dataclasses import dataclass
from typing import ClassVar

import pytest
from typing_extensions import override

from groken import cli
from groken.exec_service import (
    ExecIndeterminateError,
    ExecRemoteError,
    ExecServiceError,
)


@dataclass
class FakeResult:
    stdout: str
    stderr: str
    exit_code: int


class FakeClient:
    result: ClassVar[FakeResult] = FakeResult("out\n", "warning\n", 0)
    calls: ClassVar[list[tuple[str, str, int]]] = []

    def __init__(self) -> None:
        pass

    async def execute(
        self,
        command: str,
        working_directory: str = "/workspace",
        timeout_ms: int = 15000,
    ) -> FakeResult:
        self.calls.append((command, working_directory, timeout_ms))
        return self.result


def invoke(
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
    client: type[FakeClient] = FakeClient,
) -> int:
    FakeClient.calls = []
    monkeypatch.setattr(cli, "ExecServiceClient", client)
    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(SystemExit) as exc:
        cli.main()
    code = exc.value.code
    assert isinstance(code, int)
    return code


def test_exec_routes_arguments_and_forwards_streams(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code = invoke(
        monkeypatch,
        ["groken", "exec", "printf hi", "--cwd", "/tmp", "--timeout-ms", "42"],
    )
    captured = capsys.readouterr()
    assert code == 0
    assert captured.out == "out\n"
    assert captured.err == "warning\n"
    assert FakeClient.calls == [("printf hi", "/tmp", 42)]


def test_exec_local_path_does_not_construct_gateway_manager(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    from groken import share_client

    def unexpected_manager() -> None:
        pytest.fail("local exec constructed a gateway manager while selecting share mode")

    monkeypatch.setattr(cli, "_manager", unexpected_manager)
    monkeypatch.setattr(share_client, "load_share_link", lambda: None)

    # When
    code = invoke(monkeypatch, ["groken", "exec", "true"])

    # Then
    assert code == 0


def test_exec_success_exits_zero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class Success(FakeClient):
        @override
        async def execute(
            self,
            command: str,
            working_directory: str = "/workspace",
            timeout_ms: int = 15000,
        ) -> FakeResult:
            _ = command, working_directory, timeout_ms
            return FakeResult("ok\n", "", 0)

    assert invoke(monkeypatch, ["groken", "exec", "echo ok"], Success) == 0
    assert capsys.readouterr().out == "ok\n"


def test_exec_propagates_remote_nonzero_status(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class Failure(FakeClient):
        @override
        async def execute(
            self,
            command: str,
            working_directory: str = "/workspace",
            timeout_ms: int = 15000,
        ) -> FakeResult:
            _ = command, working_directory, timeout_ms
            return FakeResult("", "failed\n", 7)

    assert invoke(monkeypatch, ["groken", "exec", "exit 7"], Failure) == 7
    assert capsys.readouterr().err == "failed\n"


@pytest.mark.parametrize(
    "error",
    [
        ExecRemoteError("remote execution failed"),
        ExecIndeterminateError("execution state is indeterminate"),
    ],
)
def test_exec_typed_errors_exit_one(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    error: ExecServiceError,
) -> None:
    class Broken(FakeClient):
        @override
        async def execute(
            self,
            command: str,
            working_directory: str = "/workspace",
            timeout_ms: int = 15000,
        ) -> FakeResult:
            _ = command, working_directory, timeout_ms
            raise error

    assert invoke(monkeypatch, ["groken", "exec", "bad"], Broken) == 1
    assert str(error) in capsys.readouterr().err


def test_exec_rejects_empty_command(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["groken", "exec", ""])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code != 0
