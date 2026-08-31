import sys
from pathlib import Path

import pytest

from groken import cli
from groken.auth import TokenStateError
from groken.config import ConfigStateError


def test_cli_friendly_error_on_connect_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from groken.client import ConnectError

    def boom() -> None:
        raise ConnectError(401, "unauthenticated")

    monkeypatch.setattr(cli, "cmd_agents", boom)
    monkeypatch.setattr(sys, "argv", ["groken", "agents"])
    try:
        cli.main()
    except SystemExit as error:
        assert error.code == 1
    else:
        raise AssertionError("expected SystemExit")
    assert "groken login" in capsys.readouterr().err


@pytest.mark.parametrize(
    "error",
    [
        TokenStateError(Path("/private/tokens.json"), "malformed JSON"),
        ConfigStateError(Path("/private/config.json"), "expected a JSON object"),
    ],
)
def test_cli_handles_local_state_errors_without_traceback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    error: TokenStateError | ConfigStateError,
) -> None:
    # Given
    def boom() -> None:
        raise error

    monkeypatch.setattr(cli, "cmd_agents", boom)
    monkeypatch.setattr(sys, "argv", ["groken", "agents"])

    # When
    with pytest.raises(SystemExit) as raised:
        cli.main()

    # Then
    assert raised.value.code == 1
    stderr = capsys.readouterr().err
    assert str(error) in stderr
    assert "Traceback" not in stderr


def test_cli_reraises_unexpected(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom() -> None:
        raise RuntimeError("bug")

    monkeypatch.setattr(cli, "cmd_agents", boom)
    monkeypatch.setattr(sys, "argv", ["groken", "agents"])
    try:
        cli.main()
    except RuntimeError:
        pass
    else:
        raise AssertionError("expected RuntimeError to propagate")
