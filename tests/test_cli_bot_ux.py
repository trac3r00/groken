from __future__ import annotations

import sys
from typing import Any

import pytest

from groken import cli, config

AGENTS = [
    {"id": "top-id", "name": "top-bot", "isRunning": False},
    {"id": "groken-id", "name": "groken", "isRunning": True},
]


class _Manager:
    def command(self, method: str, args: object | None = None) -> list[dict[str, Any]]:
        assert method == "listAgents"
        assert args is None
        return AGENTS


def test_list_marks_machine_configured_bot(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "_manager", _Manager)
    monkeypatch.setattr(config, "cached_bot_id", lambda: "groken-id")
    monkeypatch.setattr(config, "bot_name", lambda: "groken")

    cli.cmd_list_bots()

    lines = capsys.readouterr().out.splitlines()
    assert lines == [
        "  top-id     top-bot  running=False",
        "* groken-id  groken   running=True",
    ]


def test_configure_persists_named_bot(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    remembered: list[tuple[str, str]] = []
    monkeypatch.setattr(cli, "_manager", _Manager)
    monkeypatch.setattr(config, "remember_bot", lambda bot_id, name: remembered.append((bot_id, name)))

    cli.cmd_configure("top-bot")

    assert remembered == [("top-id", "top-bot")]
    assert capsys.readouterr().out.strip() == "Configured Bot: top-bot (top-id)"


def test_configure_prompts_for_one_bot(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    remembered: list[tuple[str, str]] = []
    monkeypatch.setattr(cli, "_manager", _Manager)
    monkeypatch.setattr(config, "remember_bot", lambda bot_id, name: remembered.append((bot_id, name)))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt: "2")

    cli.cmd_configure(None)

    assert remembered == [("groken-id", "groken")]
    assert "2. groken" in capsys.readouterr().out


def test_configure_requires_bot_when_noninteractive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "_manager", _Manager)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)

    with pytest.raises(SystemExit, match="pass a Bot name or id"):
        cli.cmd_configure(None)


def test_connect_dispatches_named_bot(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[bool, int | None, str | None]] = []
    monkeypatch.setattr(cli, "cmd_vnc", lambda opened, display=None, bot=None: calls.append((opened, display, bot)))
    monkeypatch.setattr(sys, "argv", ["groken", "connect", "top-bot", "--display", "4"])

    cli._main_impl()

    assert calls == [(False, 4, "top-bot")]
