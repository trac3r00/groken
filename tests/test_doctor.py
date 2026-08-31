import subprocess
import sys
from pathlib import Path
from types import TracebackType
from typing import Never, Protocol, Self, final

import httpx
import pytest

from groken import config, doctor
from groken.auth import TokenStateError


class _Response(Protocol):
    def raise_for_status(self) -> None: ...


@final
class Response:
    def raise_for_status(self) -> None:
        return None


@final
class HealthyClient:
    def __init__(self, *, timeout: float) -> None:
        assert timeout in {2.0, 3.0, 5.0}

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc_value: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        return None

    def get(self, url: str, *, headers: dict[str, str] | None = None) -> _Response:
        assert url.startswith(("http://127.0.0.1", "https://model.test"))
        assert headers is None or headers == {}
        return Response()

    def head(self, url: str, *, headers: dict[str, str]) -> _Response:
        assert url == "https://exec.test"
        assert headers == {"authorization": "Bearer [REDACTED]"}
        return Response()


@final
class DownClient:
    def __init__(self, *, timeout: float) -> None:
        assert timeout in {2.0, 3.0}

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc_value: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        return None

    def get(self, _url: str, *, headers: dict[str, str] | None = None) -> Never:
        assert headers is None
        raise OSError("down")

    def head(self, _url: str, *, headers: dict[str, str]) -> Never:
        assert headers == {"authorization": "Bearer [REDACTED]"}
        raise OSError("down")


def successful_run(
    args: list[str],
    *,
    input: str,
    text: bool,
    capture_output: bool,
    timeout: float,
    check: bool,
) -> subprocess.CompletedProcess[str]:
    assert args == [sys.executable, "-m", "groken.mcp_server"]
    assert input.endswith("\n")
    assert text is True
    assert capture_output is True
    assert timeout == 5
    assert check is False
    return subprocess.CompletedProcess(args, 0, stdout="{}")


def failed_run(
    args: list[str],
    *,
    input: str,
    text: bool,
    capture_output: bool,
    timeout: float,
    check: bool,
) -> subprocess.CompletedProcess[str]:
    assert args == [sys.executable, "-m", "groken.mcp_server"]
    assert input.endswith("\n")
    assert text is True
    assert capture_output is True
    assert timeout == 5
    assert check is False
    return subprocess.CompletedProcess(args, 1, stdout="")


def empty_config() -> dict[str, object]:
    return {}


def unavailable_gateway() -> Never:
    raise RuntimeError("gateway unavailable")


def test_all_tiers_pass_and_never_leak_secrets(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    secret = "access-secret-123"
    monkeypatch.setattr(
        doctor, "load_tokens", lambda: {"accessToken": secret, "expiresIn": 60}
    )

    @final
    class Manager:
        def ensure_sandbox_metadata(self) -> dict[str, object]:
            return {"podId": "pod-1", "execDaemonUrl": "https://exec.test"}

        def command(self, name: str) -> list[object]:
            assert name == "listAgents"
            return []

    monkeypatch.setattr(doctor, "GatewayManager", Manager)
    monkeypatch.setattr(httpx, "Client", HealthyClient)
    monkeypatch.setattr(subprocess, "run", successful_run)

    def exec_daemon_healthy(_manager: object) -> bool:
        return True

    monkeypatch.setattr(
        doctor,
        "_exec_daemon_healthy",
        exec_daemon_healthy,
    )
    assert doctor.run_doctor() == 0
    output = capsys.readouterr().out
    assert secret not in output
    assert "https://" not in output
    assert "4 model: controller-owned (not independently probed)" in output
    assert "5 execDaemon: command ok" in output
    assert "6 podId: metadata available" in output


def test_invalid_token_state_is_reported_without_traceback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Given
    state_error = TokenStateError(tmp_path / "tokens.json", "malformed JSON")

    def invalid_tokens() -> Never:
        raise state_error

    monkeypatch.setattr(doctor, "load_tokens", invalid_tokens)
    monkeypatch.setattr(doctor, "GatewayManager", unavailable_gateway)
    monkeypatch.setattr(subprocess, "run", failed_run)

    # When
    exit_code = doctor.run_doctor()

    # Then
    output = capsys.readouterr().out
    assert exit_code == 1
    assert str(state_error) in output
    assert "Traceback" not in output


def test_invalid_config_state_is_reported_without_traceback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Given
    config_file = tmp_path / "config.json"
    _ = config_file.write_text("{broken")
    monkeypatch.setattr(config, "CONFIG_FILE", config_file)
    monkeypatch.setattr(doctor, "load_tokens", lambda: None)
    monkeypatch.setattr(doctor, "GatewayManager", unavailable_gateway)
    monkeypatch.setattr(subprocess, "run", failed_run)

    # When
    exit_code = doctor.run_doctor()

    # Then
    output = capsys.readouterr().out
    assert exit_code == 1
    assert "configuration state is invalid" in output
    assert "groken configure" in output
    assert "Traceback" not in output


def test_missing_tokens_is_hard_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "load_tokens", lambda: None)
    monkeypatch.setattr(doctor, "GatewayManager", unavailable_gateway)
    monkeypatch.setattr(subprocess, "run", failed_run)
    assert doctor.run_doctor() == 1


def test_controller_down_and_pod_metadata_remains_available(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(doctor, "load_tokens", lambda: {"accessToken": "secret"})

    @final
    class Manager:
        def ensure_sandbox_metadata(self) -> dict[str, object]:
            return {"podId": "new", "execDaemonUrl": "https://exec.test"}

        def command(self, name: str) -> list[object]:
            assert name == "listAgents"
            return []

    monkeypatch.setattr(doctor, "GatewayManager", Manager)
    monkeypatch.setattr(httpx, "Client", DownClient)
    monkeypatch.setattr(subprocess, "run", failed_run)
    assert doctor.run_doctor() == 0
    assert "6 podId: metadata available" in capsys.readouterr().out


def test_mcp_probe_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "load_tokens", lambda: {"accessToken": "secret"})
    monkeypatch.setattr(doctor, "GatewayManager", unavailable_gateway)

    def hanging(
        args: list[str],
        *,
        input: str,
        text: bool,
        capture_output: bool,
        timeout: float,
        check: bool,
    ) -> Never:
        assert args == [sys.executable, "-m", "groken.mcp_server"]
        assert input.endswith("\n")
        assert text is True
        assert capture_output is True
        assert timeout == 5
        assert check is False
        raise TimeoutError

    monkeypatch.setattr(subprocess, "run", hanging)
    assert doctor.run_doctor() == 1
