from __future__ import annotations

import anyio
import pytest
from mcp import ClientSession
from mcp.server.mcpserver.exceptions import ToolError
from mcp.shared.message import SessionMessage
from mcp.types import CallToolResult

import groken.mcp_operations as operations
import groken.mcp_server as m
from groken.client import ConnectError
from groken.mcp_support import CONFIRMATION_REQUIRED

HANDSHAKE_SECRET = "handshake-confirmation-secret"
EXPECTED_TOOLS = {
    "grok_bot_add",
    "grok_bot_ask",
    "grok_bot_capabilities",
    "grok_bot_duplicate",
    "grok_bot_list",
    "grok_bot_send",
    "grok_bot_status",
    "grok_bot_tail",
    "grok_bot_update_status",
    "grok_bot_update_trigger",
    "grok_env_capture",
    "grok_env_restore",
    "grok_plugin_call",
    "grok_plugin_list",
    "grok_routine_list",
    "grok_routine_run",
    "grok_swarm_send",
    "grok_team_ask",
    "grok_team_create",
    "grok_team_members",
}


@pytest.mark.anyio
async def test_registry_preserves_old_parity_and_adds_only_safe_operations() -> None:
    # Given / When
    tools = {tool.name: tool for tool in await m.server.list_tools()}

    # Then
    assert set(tools) == EXPECTED_TOOLS
    assert all((tool.description or "").strip() for tool in tools.values())
    assert not any(
        token in name
        for name in tools
        for token in ("delete", "room_create", "room_join", "room_leave")
    )


@pytest.mark.anyio
async def test_new_tool_schemas_pin_required_and_optional_arguments() -> None:
    # Given / When
    tools = {tool.name: tool.input_schema for tool in await m.server.list_tools()}

    # Then
    assert tools["grok_bot_update_status"]["properties"]["bot"]["default"] is None
    assert (
        tools["grok_bot_update_trigger"]["properties"]["confirmed"]["default"] is False
    )
    assert (
        tools["grok_bot_update_trigger"]["properties"]["skip_capture"]["default"]
        is False
    )
    assert tools["grok_env_capture"]["properties"]["bot"]["default"] is None
    assert tools["grok_env_restore"]["properties"]["retry_manual"]["default"] is False
    assert tools["grok_env_restore"]["properties"]["confirmed"]["default"] is False
    assert tools["grok_routine_list"]["properties"] == {}
    assert tools["grok_routine_run"]["required"] == ["name"]
    assert tools["grok_routine_run"]["properties"]["event"]["default"] == "manual"
    assert tools["grok_routine_run"]["properties"]["confirmed"]["default"] is False


@pytest.mark.anyio
async def test_memory_session_handshake_lists_and_calls_safely() -> None:
    # Given
    client_send, server_receive = anyio.create_memory_object_stream[
        SessionMessage | Exception
    ](0)
    server_send, client_receive = anyio.create_memory_object_stream[SessionMessage](0)

    # When
    with anyio.fail_after(2):
        async with client_send, server_receive, server_send, client_receive:
            async with anyio.create_task_group() as tasks:
                tasks.start_soon(
                    m.server._lowlevel_server.run,
                    server_receive,
                    server_send,
                    m.server._lowlevel_server.create_initialization_options(),
                )
                async with ClientSession(client_receive, client_send) as session:
                    initialized = await session.initialize()
                    listed = await session.list_tools()
                    blocked = await session.call_tool(
                        "grok_env_restore", {"confirmed": False}
                    )
                    invalid = await session.call_tool(
                        "grok_env_restore", {"confirmed": HANDSHAKE_SECRET}
                    )

                    # Then
                    assert initialized.server_info.name == "groken"
                    assert {tool.name for tool in listed.tools} == EXPECTED_TOOLS
                    assert blocked.structured_content == {
                        "result": CONFIRMATION_REQUIRED
                    }
                    assert invalid.is_error is True
                    rendered = invalid.model_dump_json()
                    assert HANDSHAKE_SECRET not in rendered
                    assert "confirmed must be a boolean" in rendered
                tasks.cancel_scope.cancel()


@pytest.mark.anyio
async def test_direct_dispatch_blocks_before_constructing_manager(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    def unexpected() -> None:
        raise AssertionError("manager constructed")

    monkeypatch.setattr(operations, "GatewayManager", unexpected)

    # When
    result = await m.server.call_tool(
        "grok_env_restore", {"bot": "Demo", "confirmed": False}
    )

    # Then
    assert isinstance(result, CallToolResult)
    assert result.structured_content == {"result": CONFIRMATION_REQUIRED}


@pytest.mark.anyio
async def test_direct_dispatch_returns_secret_safe_auth_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    secret = "token-private-fixture"

    def unauthorized() -> None:
        raise ConnectError(401, secret)

    monkeypatch.setattr(operations, "GatewayManager", unauthorized)

    # When
    result = await m.server.call_tool("grok_bot_update_status", {})

    # Then
    assert isinstance(result, CallToolResult)
    assert secret not in str(result.structured_content)
    assert result.structured_content == {
        "result": "grok_bot_update_status failed: gateway status 401 (authentication)."
    }


@pytest.mark.anyio
@pytest.mark.parametrize(
    "arguments",
    [
        {"name": ["not", "a", "string"], "confirmed": True},
        {"name": "demo", "event": "unknown", "confirmed": True},
    ],
)
async def test_direct_dispatch_rejects_malformed_arguments_before_tool_call(
    arguments: dict[str, str | bool | list[str]],
) -> None:
    # Given / When / Then
    with pytest.raises(ToolError, match="validation error"):
        _ = await m.server.call_tool("grok_routine_run", arguments)
