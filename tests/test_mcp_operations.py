from __future__ import annotations

import importlib
import json
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from types import ModuleType
from typing import Protocol

import pytest

import groken.mcp_server as m
from groken.bot_update import UpdateOptions
from groken.env_restore_gateway import RestoreCommandOptions
from groken.mcp_support import CONFIRMATION_REQUIRED
from groken.routines import RoutineEvent


class _ToolFunction(Protocol):
    __module__: str


class _Console(Protocol):
    def write(self, line: str) -> None: ...


class _Manager:
    pass


def _module(tool: _ToolFunction) -> ModuleType:
    return importlib.import_module(tool.__module__)


@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        ("grok_bot_update_trigger", {"bot": "Demo"}),
        ("grok_env_restore", {"bot": "Demo"}),
        ("grok_routine_run", {"name": "demo"}),
    ],
)
def test_unconfirmed_mutations_construct_nothing(
    monkeypatch: pytest.MonkeyPatch,
    tool_name: str,
    arguments: dict[str, str],
) -> None:
    # Given
    tool = getattr(m, tool_name)
    operations = _module(tool)

    def unexpected(*_args: str, **_kwargs: bool | str | None) -> None:
        raise AssertionError("unconfirmed operation constructed or called a dependency")

    for dependency in (
        "GatewayManager",
        "run_gateway_update",
        "run_gateway_restore",
        "run_routine",
    ):
        monkeypatch.setattr(operations, dependency, unexpected)

    # When
    result = tool(**arguments, confirmed=False)

    # Then
    assert result == CONFIRMATION_REQUIRED


@dataclass(frozen=True, slots=True)
class _CaptureOutcome:
    manifest_id: str
    local_path: Path
    source: StrEnum


class _CaptureSource(StrEnum):
    CHAT = "chat"


def test_update_status_uses_only_read_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given
    tool = m.grok_bot_update_status
    operations = _module(tool)

    class Manager:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, str] | None]] = []

        def resolve_agent(self, bot: str | None = None) -> str:
            assert bot == "Demo"
            return "bot-1"

        def command(
            self, method: str, args: dict[str, str] | None = None
        ) -> dict[str, bool]:
            self.calls.append((method, args))
            return {"hostUpdateAvailable": True, "imageUpdateAvailable": False}

    manager = Manager()
    monkeypatch.setattr(operations, "GatewayManager", lambda: manager)

    # When
    result = json.loads(tool("Demo"))

    # Then
    assert result == {
        "bot": "bot-1",
        "hostUpdateAvailable": True,
        "imageUpdateAvailable": False,
    }
    assert manager.calls == [("getForeverBoxStatus", {"id": "bot-1"})]


def test_read_only_capture_delegates_once(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given
    tool = m.grok_env_capture
    operations = _module(tool)
    manager = _Manager()
    calls: list[tuple[_Manager, str | None]] = []
    monkeypatch.setattr(operations, "GatewayManager", lambda: manager)

    def capture(gateway: _Manager, bot: str | None) -> _CaptureOutcome:
        calls.append((gateway, bot))
        return _CaptureOutcome("sha256:fixture", Path("/manifest"), _CaptureSource.CHAT)

    monkeypatch.setattr(operations, "capture_for_gateway", capture)

    # When
    result = tool("Demo")

    # Then
    assert calls == [(manager, "Demo")]
    assert result == "source=chat manifest_id=sha256:fixture path=/manifest"


@pytest.mark.parametrize(
    ("tool_name", "arguments", "dependency", "expected", "expected_options"),
    [
        (
            "grok_bot_update_trigger",
            {"bot": "Demo", "skip_capture": True},
            "run_gateway_update",
            "update=completed",
            UpdateOptions("Demo", yes=True, skip_capture=True),
        ),
        (
            "grok_env_restore",
            {"bot": "Demo", "retry_manual": True},
            "run_gateway_restore",
            "restore=completed",
            RestoreCommandOptions("Demo", yes=True, retry_manual=True),
        ),
    ],
)
def test_confirmed_service_mutations_dispatch_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
    tool_name: str,
    arguments: dict[str, str | bool],
    dependency: str,
    expected: str,
    expected_options: UpdateOptions | RestoreCommandOptions,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Given
    tool = getattr(m, tool_name)
    operations = _module(tool)
    manager = _Manager()
    managers: list[_Manager] = []
    calls: list[tuple[_Manager, UpdateOptions | RestoreCommandOptions]] = []

    def manager_factory() -> _Manager:
        managers.append(manager)
        return manager

    def run(
        gateway: _Manager,
        options: UpdateOptions | RestoreCommandOptions,
        console: _Console,
    ) -> None:
        calls.append((gateway, options))
        console.write(expected)

    monkeypatch.setattr(operations, "GatewayManager", manager_factory)
    monkeypatch.setattr(operations, dependency, run)

    # When
    result = tool(**arguments, confirmed=True)

    # Then
    assert result == expected
    assert managers == [manager]
    assert calls == [(manager, expected_options)]
    assert capsys.readouterr().out == ""


def test_routine_list_is_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given
    monkeypatch.setenv("HOME", str(tmp_path))

    # When
    names = json.loads(m.grok_routine_list())

    # Then
    assert names == ["env-capture", "env-restore"]
    assert not (tmp_path / ".config").exists()


def test_confirmed_routine_runs_exactly_once(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given
    tool = m.grok_routine_run
    operations = _module(tool)
    calls: list[tuple[str, RoutineEvent]] = []

    def run(name: str, event: RoutineEvent) -> int:
        calls.append((name, event))
        return 7

    monkeypatch.setattr(operations, "run_routine", run)

    # When
    result = tool("demo", confirmed=True)

    # Then
    assert json.loads(result) == {"event": "manual", "exit_code": 7, "routine": "demo"}
    assert len(calls) == 1
    assert calls[0][0] == "demo"
