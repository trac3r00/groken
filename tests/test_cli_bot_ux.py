from __future__ import annotations

import sys

import pytest

from groken import cli, config, installers

AGENTS: list[dict[str, object]] = [
    {"id": "top-id", "name": "top-bot", "isRunning": False},
    {"id": "groken-id", "name": "groken", "isRunning": True},
]


class _Manager:
    def command(
        self, method: str, args: dict[str, object] | None = None
    ) -> list[dict[str, object]]:
        assert method == "listAgents"
        assert args is None
        return AGENTS


def test_bot_update_dispatch_preserves_arguments_and_help(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Given
    calls: list[tuple[str | None, bool, bool]] = []

    def update(bot: str | None, *, yes: bool, skip_capture: bool) -> None:
        calls.append((bot, yes, skip_capture))

    monkeypatch.setattr(cli, "cmd_bot_update", update)
    monkeypatch.setattr(
        sys, "argv", ["groken", "bot", "update", "Demo", "--yes", "--skip-capture"]
    )

    # When
    cli.main()

    # Then
    assert calls == [("Demo", True, True)]
    monkeypatch.setattr(sys, "argv", ["groken", "bot", "update", "--help"])
    with pytest.raises(SystemExit, match="0"):
        cli.main()
    help_text = capsys.readouterr().out
    assert all(value in help_text for value in ("BOT", "--yes", "--skip-capture"))


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

    def remember_bot(bot_id: str, name: str) -> None:
        remembered.append((bot_id, name))

    monkeypatch.setattr(cli, "_manager", _Manager)
    monkeypatch.setattr(config, "remember_bot", remember_bot)
    monkeypatch.setattr(
        installers, "install_cli_command", lambda dry_run: "dry/cli/path"
    )

    cli.cmd_configure("top-bot")

    assert remembered == [("top-id", "top-bot")]
    assert capsys.readouterr().out.strip() == (
        "Configured Bot: top-bot (top-id)\nCLI command: dry/cli/path"
    )


def test_configure_prompts_for_one_bot(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    remembered: list[tuple[str, str]] = []

    def remember_bot(bot_id: str, name: str) -> None:
        remembered.append((bot_id, name))

    def select_second_bot(_prompt: str) -> str:
        return "2"

    monkeypatch.setattr(cli, "_manager", _Manager)
    monkeypatch.setattr(config, "remember_bot", remember_bot)
    monkeypatch.setattr(
        "groken.installers.install_cli_command", lambda dry_run: "already present"
    )
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", select_second_bot)

    cli.cmd_configure(None)

    assert remembered == [("groken-id", "groken")]
    assert "2. groken" in capsys.readouterr().out


def test_configure_requires_bot_when_noninteractive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "_manager", _Manager)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)

    with pytest.raises(SystemExit, match="pass a Bot name or id"):
        cli.cmd_configure(None)


def test_connect_dispatches_named_bot(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[bool, int | None, str | None]] = []

    def capture_vnc(
        opened: bool, display: int | None = None, bot: str | None = None
    ) -> None:
        calls.append((opened, display, bot))

    monkeypatch.setattr(cli, "cmd_vnc", capture_vnc)
    monkeypatch.setattr(sys, "argv", ["groken", "connect", "top-bot", "--display", "4"])

    cli.main()

    assert calls == [(False, 4, "top-bot")]
