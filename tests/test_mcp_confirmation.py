from __future__ import annotations

from dataclasses import dataclass
from typing import TypeAlias

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult

import groken.mcp_operations as operations
import groken.mcp_server as m
from groken.bot_update import UpdateOptions
from groken.client import ConnectError
from groken.env_restore_gateway import RestoreCommandOptions
from groken.routines import RoutineEvent

JsonValue: TypeAlias = (
    None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]
)
INVALID_CONFIRMATIONS: tuple[JsonValue, ...] = (None, "yes", "true", 1, [], {})
SECRET = "confirmation-secret-sentinel"
CONFIRMATION_REQUIRED = (
    "Operation blocked: review the exact operation with the user, then retry with "
    "confirmed=true."
)


@dataclass(frozen=True, slots=True)
class MutationCase:
    name: str
    arguments: dict[str, JsonValue]


CASES = (
    MutationCase("grok_bot_add", {"name": "demo"}),
    MutationCase("grok_bot_duplicate", {"source": "demo", "name": "copy"}),
    MutationCase("grok_team_create", {"name": "team", "bots": ["a", "b"]}),
    MutationCase("grok_plugin_call", {"server": "user-X", "tool": "search"}),
    MutationCase("grok_bot_update_trigger", {}),
    MutationCase("grok_env_restore", {}),
    MutationCase("grok_routine_run", {"name": "demo"}),
)


def _direct(case: MutationCase, confirmed: JsonValue) -> str:
    tool = getattr(m, case.name)
    return tool(**case.arguments, confirmed=confirmed)


class _Console:
    def write(self, _line: str) -> None:
        return None


class _Manager:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def create_bot(self, name: str) -> dict[str, JsonValue]:
        self.calls.append("grok_bot_add")
        return {"id": "new-id", "name": name}

    def duplicate_bot(self, source_name: str, name: str) -> dict[str, JsonValue]:
        del source_name
        self.calls.append("grok_bot_duplicate")
        return {"id": "copy-id", "name": name}

    def command(
        self, method: str, _args: dict[str, JsonValue] | None = None
    ) -> JsonValue:
        assert method == "listAgents"
        return [
            {"id": "a-id", "name": "a", "isGroup": False},
            {"id": "b-id", "name": "b", "isGroup": False},
        ]

    def command_once(self, method: str, args: dict[str, JsonValue]) -> JsonValue:
        assert method == "createGroup"
        self.calls.append("grok_team_create")
        return {
            "agent": {
                "id": "team-id",
                "name": args["name"],
                "isGroup": True,
                "memberIds": args["memberAgentIds"],
            }
        }

    def resolve_agent(self, bot: str | None = None) -> str:
        del bot
        return "bot-1"


class _Client:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def list_sand_mcp_tools(self, _servers: list[str]) -> dict[str, JsonValue]:
        return {
            "servers": [
                {
                    "serverIdentifier": "user-X",
                    "tools": [{"name": "search", "toolName": "search"}],
                }
            ]
        }

    def execute_sand_mcp_tool(
        self,
        *,
        server_identifier: str,
        tool_name: str,
        arguments: dict[str, JsonValue],
        tool_call_id: str,
        agent_id: str,
    ) -> dict[str, JsonValue]:
        del server_identifier, tool_name, arguments, tool_call_id, agent_id
        self.calls.append("grok_plugin_call")
        return {"ok": True}


def _install_success(
    monkeypatch: pytest.MonkeyPatch,
) -> list[str]:
    calls: list[str] = []
    monkeypatch.setattr(m, "GatewayManager", lambda: _Manager(calls))
    monkeypatch.setattr(m, "SandClient", lambda: _Client(calls))
    monkeypatch.setattr(operations, "GatewayManager", lambda: _Manager(calls))

    def update(_gateway: _Manager, _options: UpdateOptions, _console: _Console) -> None:
        calls.append("grok_bot_update_trigger")

    def restore(
        _gateway: _Manager, _options: RestoreCommandOptions, _console: _Console
    ) -> None:
        calls.append("grok_env_restore")

    def routine(_name: str, _event: RoutineEvent) -> int:
        calls.append("grok_routine_run")
        return 0

    monkeypatch.setattr(operations, "run_gateway_update", update)
    monkeypatch.setattr(operations, "run_gateway_restore", restore)
    monkeypatch.setattr(operations, "run_routine", routine)
    return calls


def _block_dependencies(monkeypatch: pytest.MonkeyPatch) -> None:
    def unexpected(*_args: JsonValue, **_kwargs: JsonValue) -> None:
        raise AssertionError("confirmation reached a dependency")

    for module, names in (
        (m, ("GatewayManager", "SandClient")),
        (
            operations,
            (
                "GatewayManager",
                "run_gateway_update",
                "run_gateway_restore",
                "run_routine",
            ),
        ),
    ):
        for name in names:
            monkeypatch.setattr(module, name, unexpected)


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
@pytest.mark.parametrize("confirmed", INVALID_CONFIRMATIONS, ids=repr)
def test_direct_invalid_confirmation_is_sanitized_before_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    case: MutationCase,
    confirmed: JsonValue,
) -> None:
    # Given
    _block_dependencies(monkeypatch)

    # When / Then
    with pytest.raises(TypeError, match="^confirmed must be a boolean$"):
        _ = _direct(case, confirmed)


@pytest.mark.anyio
@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
@pytest.mark.parametrize("confirmed", INVALID_CONFIRMATIONS, ids=repr)
async def test_mcp_invalid_confirmation_is_sanitized_before_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    case: MutationCase,
    confirmed: JsonValue,
) -> None:
    # Given
    _block_dependencies(monkeypatch)

    # When
    with pytest.raises(ToolError) as raised:
        _ = await m.server.call_tool(
            case.name,
            {**case.arguments, "confirmed": confirmed},
        )

    # Then
    assert str(raised.value) == (
        f"Error executing tool {case.name}: confirmed must be a boolean"
    )
    assert repr(confirmed) not in str(raised.value)
    assert "input_value" not in str(raised.value)


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
def test_direct_false_returns_common_block_without_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    case: MutationCase,
) -> None:
    # Given
    _block_dependencies(monkeypatch)

    # When / Then
    assert _direct(case, False) == CONFIRMATION_REQUIRED


@pytest.mark.anyio
@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
async def test_mcp_false_returns_common_block_without_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    case: MutationCase,
) -> None:
    # Given
    _block_dependencies(monkeypatch)

    # When
    result = await m.server.call_tool(
        case.name,
        {**case.arguments, "confirmed": False},
    )

    # Then
    assert isinstance(result, CallToolResult)
    assert result.structured_content == {"result": CONFIRMATION_REQUIRED}


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
def test_direct_true_executes_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
    case: MutationCase,
) -> None:
    # Given
    calls = _install_success(monkeypatch)

    # When
    _ = _direct(case, True)

    # Then
    assert calls == [case.name]


@pytest.mark.anyio
@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
async def test_mcp_true_executes_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
    case: MutationCase,
) -> None:
    # Given
    calls = _install_success(monkeypatch)

    # When
    _ = await m.server.call_tool(
        case.name,
        {**case.arguments, "confirmed": True},
    )

    # Then
    assert calls == [case.name]


@pytest.mark.anyio
@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
async def test_confirmation_schema_stays_strictly_boolean(case: MutationCase) -> None:
    # Given / When
    tools = {tool.name: tool for tool in await m.server.list_tools()}

    # Then
    assert tools[case.name].input_schema["properties"]["confirmed"] == {
        "default": False,
        "title": "Confirmed",
        "type": "boolean",
    }


@pytest.mark.anyio
async def test_invalid_confirmation_secret_never_appears_in_mcp_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    _block_dependencies(monkeypatch)

    # When
    with pytest.raises(ToolError) as raised:
        _ = await m.server.call_tool(
            "grok_bot_add",
            {"name": "demo", "confirmed": SECRET},
        )

    # Then
    assert SECRET not in str(raised.value)
    assert str(raised.value).endswith("confirmed must be a boolean")
    assert "input_value" not in str(raised.value)


@pytest.mark.anyio
async def test_configuration_path_is_sanitized_through_mcp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    def fail() -> None:
        raise SystemExit(f"missing /private/{SECRET}/config.json")

    monkeypatch.setattr(m, "GatewayManager", fail)

    # When
    result = await m.server.call_tool(
        "grok_bot_add", {"name": "demo", "confirmed": True}
    )

    # Then
    assert isinstance(result, CallToolResult)
    rendered = str(result.structured_content)
    assert SECRET not in rendered
    assert "/private/" not in rendered
    assert result.structured_content == {
        "result": "grok_bot_add failed: local configuration error."
    }


@pytest.mark.anyio
@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
async def test_connect_error_body_is_sanitized_through_mcp(
    monkeypatch: pytest.MonkeyPatch,
    case: MutationCase,
) -> None:
    # Given
    def fail(*_args: JsonValue, **_kwargs: JsonValue) -> None:
        raise ConnectError(503, f"token={SECRET} path=/private/{SECRET}")

    monkeypatch.setattr(m, "GatewayManager", fail)
    monkeypatch.setattr(m, "SandClient", fail)
    monkeypatch.setattr(operations, "GatewayManager", fail)
    monkeypatch.setattr(operations, "run_routine", fail)

    # When
    result = await m.server.call_tool(
        case.name,
        {**case.arguments, "confirmed": True},
    )

    # Then
    assert isinstance(result, CallToolResult)
    rendered = str(result.structured_content)
    assert SECRET not in rendered
    assert "/private/" not in rendered
    assert case.name in rendered
    assert "503" in rendered
