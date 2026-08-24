import sys
from dataclasses import dataclass
from typing import ClassVar

import pytest

from groken import cli
from groken.exec_service import ExecIndeterminateError, ExecRemoteError


@dataclass
class FakeResult:
    stdout: str
    stderr: str


class FakeClient:
    result = FakeResult("out\n", "err\n")
    calls: ClassVar[list] = []

    def __init__(self):
        pass

    async def execute(self, command, working_directory="/workspace", timeout_ms=15000):
        self.calls.append((command, working_directory, timeout_ms))
        return self.result


def invoke(monkeypatch, argv, client=FakeClient):
    FakeClient.calls = []
    monkeypatch.setattr(cli, "ExecServiceClient", client)
    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(SystemExit) as exc:
        cli.main()
    return exc.value.code


def test_exec_routes_arguments_and_forwards_streams(monkeypatch, capsys):
    code = invoke(monkeypatch, ["groken", "exec", "printf hi", "--cwd", "/tmp", "--timeout-ms", "42"])
    captured = capsys.readouterr()
    assert code == 1  # ExecResult.stderr denotes a remote command failure.
    assert captured.out == "out\n"
    assert captured.err == "err\n"
    assert FakeClient.calls == [("printf hi", "/tmp", 42)]


def test_exec_success_exits_zero(monkeypatch, capsys):
    class Success(FakeClient):
        async def execute(self, *args, **kwargs):
            return FakeResult("ok\n", "")

    assert invoke(monkeypatch, ["groken", "exec", "echo ok"], Success) == 0
    assert capsys.readouterr().out == "ok\n"


@pytest.mark.parametrize("error", [ExecRemoteError("remote execution failed"), ExecIndeterminateError("execution state is indeterminate")])
def test_exec_typed_errors_exit_one(monkeypatch, capsys, error):
    class Broken(FakeClient):
        async def execute(self, *args, **kwargs):
            raise error

    assert invoke(monkeypatch, ["groken", "exec", "bad"], Broken) == 1
    assert str(error) in capsys.readouterr().err


def test_exec_rejects_empty_command(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["groken", "exec", ""])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code != 0
