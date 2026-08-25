from __future__ import annotations

import sys
from typing import Any, ClassVar

import pytest

from groken import cli
from groken.plugin_tools import resolve_catalog_tool_name

DISCOVERY = {
    "servers": [
        {
            "serverIdentifier": "user-X",
            "status": "connected",
            "accountLabel": "default",
            "tools": [
                {
                    "name": "search_x",
                    "toolName": "search",
                    "providerIdentifier": "user-X",
                    "description": "Search public X posts",
                    "inputSchema": {"type": "object"},
                }
            ],
        }
    ]
}


class _Client:
    calls: ClassVar[list[tuple[object, ...]]] = []

    def list_sand_mcp_tools(self, servers: list[str]) -> dict[str, Any]:
        self.calls.append(("list", servers))
        return DISCOVERY

    def execute_sand_mcp_tool(self, **kwargs: object) -> dict[str, Any]:
        self.calls.append(("call", kwargs))
        return {"result": {"result": {"case": "success", "value": {"content": []}}}}


class _Manager:
    def resolve_agent(self, bot: str | None) -> str:
        return f"resolved:{bot}"


def test_short_tool_name_stays_with_selected_server() -> None:
    payload = {
        "servers": [
            {"serverIdentifier": "user-Gmail", "tools": [{"name": "user-Gmail-search", "toolName": "search"}]},
            {"serverIdentifier": "user-Gmail--work", "tools": [{"name": "user-Gmail--work-search", "toolName": "search"}]},
        ]
    }

    assert resolve_catalog_tool_name(payload, "search", "user-Gmail") == "user-Gmail-search"


def test_tools_list_prints_server_and_schema(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    _Client.calls = []
    monkeypatch.setattr(cli, "SandClient", _Client)

    cli.cmd_tools_list(["user-X"], as_json=False)

    assert _Client.calls == [("list", ["user-X"])]
    output = capsys.readouterr().out
    assert "user-X [connected]" in output
    assert "search_x" in output
    assert "Search public X posts" in output


def test_tools_list_json_preserves_machine_schema(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(cli, "SandClient", _Client)

    cli.cmd_tools_list([], as_json=True)

    assert '"inputSchema"' in capsys.readouterr().out


def test_tools_call_requires_explicit_confirmation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "SandClient", _Client)
    monkeypatch.setattr(cli, "_manager", _Manager)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)

    with pytest.raises(SystemExit, match="requires --yes"):
        cli.cmd_tools_call("user-X", "search", '{"query":"grok"}', None, yes=False)


def test_tools_call_executes_confirmed_json(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    _Client.calls = []
    monkeypatch.setattr(cli, "SandClient", _Client)
    monkeypatch.setattr(cli, "_manager", _Manager)
    monkeypatch.setattr(cli.uuid, "uuid4", lambda: "call-1")

    cli.cmd_tools_call("user-X", "search", '{"query":"grok"}', "groken", yes=True)

    assert _Client.calls == [
        ("list", ["user-X"]),
        ("call", {
        "server_identifier": "user-X",
        "tool_name": "search_x",
        "arguments": {"query": "grok"},
        "tool_call_id": "call-1",
        "agent_id": "resolved:groken",
    }),
    ]
    assert '"case": "success"' in capsys.readouterr().out


def test_tools_call_rejects_non_object_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "SandClient", _Client)
    monkeypatch.setattr(cli, "_manager", _Manager)

    with pytest.raises(SystemExit, match="JSON object"):
        cli.cmd_tools_call("user-X", "search", "[]", None, yes=True)
