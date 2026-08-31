from __future__ import annotations

import multiprocessing
import subprocess
import sys
import warnings
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from multiprocessing.context import BaseContext
from typing import TypeAlias

import pytest

from groken import cli, swarm
from groken.swarm_process import (
    DirectRoundExecutor,
    SubprocessRoundExecutor,
    launch_worker,
)

TASK = "summarize the repo"
JsonValue: TypeAlias = (
    None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]
)


def _use_direct_executor(
    monkeypatch: pytest.MonkeyPatch, manager: InterfaceManager
) -> None:
    import groken.swarm_process as process

    monkeypatch.setattr(
        process, "SubprocessRoundExecutor", lambda: DirectRoundExecutor(manager)
    )


class InterfaceManager:
    def __init__(
        self,
        replies: dict[str, str | BaseException],
        sharing: JsonValue = None,
    ) -> None:
        self.replies = replies
        self.sharing = sharing
        self.agents: list[JsonValue] = [
            {"id": f"{name}-id", "name": name, "isRunning": True} for name in replies
        ]
        self.commands: list[tuple[str, dict[str, JsonValue] | None]] = []
        context = multiprocessing.get_context("fork")
        self._ask_receive, self._ask_send = context.Pipe(duplex=False)
        self._ask_lock = context.Lock()
        self._asks: list[tuple[str, str, float]] = []

    @property
    def asks(self) -> list[tuple[str, str, float]]:
        while self._ask_receive.poll():
            self._asks.append(self._ask_receive.recv())
        return self._asks

    def command(
        self, method: str, args: dict[str, JsonValue] | None = None
    ) -> JsonValue:
        self.commands.append((method, args))
        if method == "listAgents":
            return list(self.agents)
        if method == "getSharingState":
            return self.sharing
        raise AssertionError(f"unexpected command {method}")

    def ask(self, agent_id: str, text: str, timeout_s: float = 600) -> str:
        with self._ask_lock:
            self._ask_send.send((agent_id, text, timeout_s))
        scripted = self.replies[agent_id.removesuffix("-id")]
        if isinstance(scripted, BaseException):
            raise scripted
        return scripted


def test_rooms_uses_only_read_commands_and_declares_read_only() -> None:
    # Given
    manager = InterfaceManager(
        {"a": "a answer"},
        sharing={
            "rooms": [
                {
                    "roomId": "room-1",
                    "name": "Shipping",
                    "members": [
                        {"kind": "agent", "name": "alpha"},
                        {"kind": "user", "name": "bob"},
                    ],
                }
            ]
        },
    )

    # When
    rendered = swarm.render_rooms(swarm.read_rooms(manager))

    # Then
    assert [method for method, _ in manager.commands] == ["getSharingState"]
    assert all(value in rendered for value in ("room-1", "Shipping", "alpha"))
    assert "never creates, joins, or leaves" in rendered
    assert manager.asks == []


def test_rooms_reports_valid_empty_state_without_mutating() -> None:
    # Given
    manager = InterfaceManager({"a": "a answer"}, sharing={"rooms": []})

    # When
    rendered = swarm.render_rooms(swarm.read_rooms(manager))

    # Then
    assert "no shared rooms" in rendered.lower()
    assert [method for method, _ in manager.commands] == ["getSharingState"]


def test_cli_swarm_send_prints_sections_and_exits_zero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Given
    manager = InterfaceManager({"a": "a answer", "b": "b answer", "c": "c answer"})
    _use_direct_executor(monkeypatch, manager)
    monkeypatch.setattr(cli, "_manager", lambda: manager)
    monkeypatch.setattr(
        sys, "argv", ["groken", "swarm", "send", "--bots", "a,b,c", TASK]
    )

    # When
    with pytest.raises(SystemExit) as exit_info:
        cli.main()

    # Then
    assert exit_info.value.code == 0
    out = capsys.readouterr().out
    assert out.index("=== a ===") < out.index("=== b ===") < out.index("=== c ===")
    assert [call[1] for call in manager.asks] == [TASK, TASK, TASK]


def test_cli_swarm_send_exits_one_when_every_bot_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Given
    manager = InterfaceManager({"a": RuntimeError("boom")})
    _use_direct_executor(monkeypatch, manager)
    monkeypatch.setattr(cli, "_manager", lambda: manager)
    monkeypatch.setattr(sys, "argv", ["groken", "swarm", "send", "--bots", "a", TASK])

    # When
    with pytest.raises(SystemExit) as exit_info:
        cli.main()

    # Then
    assert exit_info.value.code == 1
    assert "boom" in capsys.readouterr().out


def test_cli_swarm_rooms_prints_read_only_summary(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Given
    manager = InterfaceManager(
        {"a": "a answer"}, sharing={"rooms": [{"roomId": "room-9", "members": []}]}
    )
    monkeypatch.setattr(cli, "_manager", lambda: manager)
    monkeypatch.setattr(sys, "argv", ["groken", "swarm", "rooms"])

    # When
    cli.main()

    # Then
    out = capsys.readouterr().out
    assert "room-9" in out
    assert "never creates, joins, or leaves" in out
    assert [method for method, _ in manager.commands] == ["getSharingState"]


def test_cli_swarm_rooms_reports_malformed_state_without_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Given
    manager = InterfaceManager({"a": "a answer"}, sharing={"rooms": None})
    monkeypatch.setattr(cli, "_manager", lambda: manager)
    monkeypatch.setattr(sys, "argv", ["groken", "swarm", "rooms"])

    # When
    with pytest.raises(SystemExit) as exit_info:
        cli.main()

    # Then
    assert exit_info.value.code == 1
    assert "malformed" in capsys.readouterr().err
    assert [method for method, _ in manager.commands] == ["getSharingState"]


def test_cli_swarm_selection_error_is_reported_with_exit_one(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Given
    manager = InterfaceManager({"a": "a answer"})
    _use_direct_executor(monkeypatch, manager)
    monkeypatch.setattr(cli, "_manager", lambda: manager)
    monkeypatch.setattr(
        sys, "argv", ["groken", "swarm", "send", "--bots", "a,ghost", TASK]
    )

    # When
    with pytest.raises(SystemExit) as exit_info:
        cli.main()

    # Then
    assert exit_info.value.code == 1
    assert "unknown bot" in capsys.readouterr().err
    assert manager.asks == []


def test_cli_swarm_help_lists_flags(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Given
    monkeypatch.setattr(sys, "argv", ["groken", "swarm", "send", "--help"])

    # When
    with pytest.raises(SystemExit, match="0"):
        cli.main()

    # Then
    help_text = capsys.readouterr().out
    assert all(
        flag in help_text for flag in ("--bots", "--exclude", "--timeout-s", "--rounds")
    )


@pytest.mark.anyio
async def test_mcp_swarm_tool_is_registered_with_description() -> None:
    # Given
    import groken.mcp_server as m

    # When
    tools = {tool.name: tool for tool in await m.server.list_tools()}

    # Then
    assert "grok_swarm_send" in tools
    assert (tools["grok_swarm_send"].description or "").strip()


def test_mcp_thread_uses_fresh_interpreter_without_fork_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a real subprocess adapter whose fresh worker hangs
    import groken.mcp_server as m

    manager = InterfaceManager({"a": "parent manager must not be asked"})

    class RecordingLauncher:
        def __init__(self) -> None:
            self.processes: list[subprocess.Popen[bytes]] = []

        def __call__(self, command: Sequence[str]) -> subprocess.Popen[bytes]:
            process = launch_worker(command)
            self.processes.append(process)
            return process

    launcher = RecordingLauncher()
    executor = SubprocessRoundExecutor(
        command=(
            sys.executable,
            "-c",
            "import sys,time; sys.stdin.buffer.read(); time.sleep(60)",
        ),
        launcher=launcher,
    )
    monkeypatch.setattr(m, "GatewayManager", lambda: manager)
    monkeypatch.setattr(m, "SubprocessRoundExecutor", lambda: executor, raising=False)
    original_get_context = multiprocessing.get_context

    def reject_fork(method: str | None = None) -> BaseContext:
        assert method != "fork", "MCP dispatch attempted a multithreaded fork"
        return original_get_context(method)

    monkeypatch.setattr(multiprocessing, "get_context", reject_fork)

    # When MCP runs its synchronous tool from a worker thread
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")
        with ThreadPoolExecutor(max_workers=1) as pool:
            rendered = pool.submit(
                m.grok_swarm_send, TASK, ["a"], None, 0.05, 1
            ).result(timeout=2)

    # Then
    assert "FAILED: orchestration timed out after 0.05s" in rendered
    assert not any("fork" in str(item.message).lower() for item in captured)
    assert manager.asks == []
    assert launcher.processes
    assert all(process.poll() is not None for process in launcher.processes)


def test_mcp_swarm_send_matches_cli_section_format(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    import groken.mcp_server as m

    manager = InterfaceManager({"a": "a answer", "b": RuntimeError("b down")})
    monkeypatch.setattr(m, "GatewayManager", lambda: manager)
    monkeypatch.setattr(
        m, "SubprocessRoundExecutor", lambda: DirectRoundExecutor(manager)
    )

    # When
    rendered = m.grok_swarm_send(TASK, ["a", "b"], timeout_s=30)

    # Then
    assert rendered.index("=== a ===") < rendered.index("=== b ===")
    assert all(value in rendered for value in ("a answer", "FAILED", "b down"))
    assert [call[2] for call in manager.asks] == [30.0, 30.0]
