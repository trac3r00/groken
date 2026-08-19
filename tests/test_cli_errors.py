import sys

import groken.cli as cli


def test_cli_friendly_error_on_connect_error(monkeypatch, capsys):
    from groken.client import ConnectError

    def boom():
        raise ConnectError(401, "unauthenticated")

    monkeypatch.setattr(cli, "cmd_agents", boom)
    monkeypatch.setattr(sys, "argv", ["groken", "agents"])
    try:
        cli.main()
    except SystemExit as e:
        assert e.code == 1
    else:
        raise AssertionError("expected SystemExit")
    assert "groken login" in capsys.readouterr().err


def test_cli_reraises_unexpected(monkeypatch):
    def boom():
        raise RuntimeError("bug")

    monkeypatch.setattr(cli, "cmd_agents", boom)
    monkeypatch.setattr(sys, "argv", ["groken", "agents"])
    try:
        cli.main()
    except RuntimeError:
        pass
    else:
        raise AssertionError("expected RuntimeError to propagate")
