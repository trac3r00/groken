import asyncio
import json
import logging
import uuid
from typing import Protocol, cast

from mcp.server.mcpserver import MCPServer

from . import mcp_operations, mcp_support
from .capabilities import capability_manifest, live_read_only_status
from .client import SandClient
from .gateway import GatewayManager
from .swarm_process import SubprocessRoundExecutor

server = MCPServer("groken")

JsonObject = dict[str, object]
OPERATION_TOOLS = mcp_operations.OPERATION_TOOLS
_translate_async_tool_errors = mcp_support.translate_async_tool_errors
_translate_tool_errors = mcp_support.translate_tool_errors
grok_bot_update_status = mcp_operations.grok_bot_update_status
grok_bot_update_trigger = mcp_operations.grok_bot_update_trigger
grok_env_capture = mcp_operations.grok_env_capture
grok_env_restore = mcp_operations.grok_env_restore
grok_routine_list = mcp_operations.grok_routine_list
grok_routine_run = mcp_operations.grok_routine_run


class AgentResolver(Protocol):
    def resolve_agent(self, bot: str | None = None) -> str: ...


class Gateway(AgentResolver, Protocol):
    def command(self, method: str, args: JsonObject | None = None) -> object: ...
    def create_bot(self, name: str) -> JsonObject: ...
    def duplicate_bot(self, source_name: str, name: str) -> JsonObject: ...
    def send_prompt(
        self, agent_id: str, text: str, client_nonce: str | None = None
    ) -> object: ...
    def transcript_tail(self, agent_id: str) -> object: ...
    def ask(
        self,
        agent_id: str,
        text: str,
        timeout_s: float = 600,
        idle_s: float = 45,
    ) -> str: ...


def _object_dict(value: object) -> JsonObject | None:
    if not isinstance(value, dict):
        return None
    result: JsonObject = {}
    for key, item in cast("dict[object, object]", value).items():
        if not isinstance(key, str):
            return None
        result[key] = item
    return result


def _resolve(mgr: AgentResolver, bot: str | None) -> str:
    return mgr.resolve_agent(bot)


@_translate_tool_errors
def grok_bot_list() -> str:
    """List the user's Grok Bots with id, name, and running state."""
    manager: Gateway = GatewayManager()
    raw_agents = manager.command("listAgents")
    if not isinstance(raw_agents, list):
        raise TypeError("listAgents response is not a list")
    body: list[JsonObject] = []
    for raw_agent in cast("list[object]", raw_agents):
        agent = _object_dict(raw_agent)
        if agent is None:
            raise TypeError("listAgents response contains a non-object")
        body.append(
            {
                "id": agent.get("id"),
                "name": agent.get("name"),
                "running": agent.get("isRunning"),
            }
        )
    return json.dumps(body, ensure_ascii=False, indent=2)


@_translate_tool_errors
def grok_bot_add(name: str, confirmed: mcp_support.Confirmation = False) -> str:
    """Create a Grok Bot with the worker guardrail only after explicit confirmation."""
    if not mcp_support.require_confirmation(confirmed):
        return mcp_support.CONFIRMATION_REQUIRED
    manager: Gateway = GatewayManager()
    return json.dumps(manager.create_bot(name), ensure_ascii=False, indent=2)


@_translate_tool_errors
def grok_bot_duplicate(
    source: str,
    name: str,
    confirmed: mcp_support.Confirmation = False,
) -> str:
    """Duplicate a Grok Bot under a new name only after explicit confirmation."""
    if not mcp_support.require_confirmation(confirmed):
        return mcp_support.CONFIRMATION_REQUIRED
    manager: Gateway = GatewayManager()
    return json.dumps(manager.duplicate_bot(source, name), ensure_ascii=False, indent=2)


@_translate_tool_errors
def grok_team_create(
    name: str,
    bots: list[str],
    description: str = "",
    confirmed: mcp_support.Confirmation = False,
) -> str:
    """Create a persistent native Grok Bot team only after explicit confirmation."""
    from .native_teams import NativeTeamGateway, create_native_team

    if not mcp_support.require_confirmation(confirmed):
        return mcp_support.CONFIRMATION_REQUIRED
    manager = cast("NativeTeamGateway", cast(object, GatewayManager()))
    team = create_native_team(manager, name, bots, description)
    return json.dumps(
        {
            "id": team.team_id,
            "name": team.name,
            "description": team.description,
            "members": [
                {"id": member.agent_id, "name": member.name} for member in team.members
            ],
        },
        ensure_ascii=False,
        indent=2,
    )


@_translate_tool_errors
def grok_team_members(team: str) -> str:
    """List the ordered Bot members of one persistent native Grok Bot team."""
    from .native_teams import NativeTeamGateway, get_native_team

    manager = cast("NativeTeamGateway", cast(object, GatewayManager()))
    found = get_native_team(manager, team)
    return json.dumps(
        {
            "id": found.team_id,
            "name": found.name,
            "description": found.description,
            "members": [
                {"id": member.agent_id, "name": member.name} for member in found.members
            ],
        },
        ensure_ascii=False,
        indent=2,
    )


@_translate_async_tool_errors
async def grok_team_ask(team: str, text: str, timeout_s: float = 600) -> str:
    """Send one task to a native Grok Bot team and wait for its coordinated reply."""
    from .native_teams import NativeTeamGateway, ask_native_team

    manager = cast("NativeTeamGateway", cast(object, GatewayManager()))
    return await asyncio.to_thread(ask_native_team, manager, team, text, timeout_s)


@_translate_tool_errors
def grok_bot_send(text: str, bot: str | None = None) -> str:
    """Send a message to a Grok Bot without waiting for the reply. Defaults to the dedicated groken Bot (auto-created on first use); pass bot (name or id) only to target a specific other Bot."""
    mgr: Gateway = GatewayManager()
    _ = mgr.send_prompt(_resolve(mgr, bot), text)
    return "sent"


@_translate_async_tool_errors
async def grok_bot_ask(
    text: str, bot: str | None = None, timeout_s: float = 600, idle_s: float = 45
) -> str:
    """Send a message to a Grok Bot and return its reply text. Defaults to the dedicated groken Bot (auto-created on first use); pass bot (name or id) only to target a specific other Bot."""
    mgr: Gateway = GatewayManager()
    agent_id = _resolve(mgr, bot)
    reply = await asyncio.to_thread(mgr.ask, agent_id, text, timeout_s, idle_s)
    return reply or "(no reply text received)"


@_translate_tool_errors
def grok_bot_status() -> str:
    """Show the configured Bot, host, secrets, connected MCP, and local health."""
    from .status import collect_status

    return json.dumps(
        collect_status(GatewayManager()),
        ensure_ascii=False,
        indent=2,
    )


@_translate_tool_errors
def grok_bot_capabilities(include_commands: bool = False) -> str:
    """Inspect the official app's typed gateway surface plus safe live feature gates."""
    manager = GatewayManager()
    payload = capability_manifest(include_commands=include_commands)
    payload["live"] = live_read_only_status(manager)
    return json.dumps(payload, ensure_ascii=False, indent=2)


@_translate_tool_errors
def grok_plugin_list(server: str | None = None) -> str:
    """List backend tools from the user's connected Grok Bot plugins. Optionally filter by server identifier such as user-Gmail."""
    from .plugin_tools import render_tool_catalog

    payload = SandClient().list_sand_mcp_tools([] if server is None else [server])
    return render_tool_catalog(payload)


@_translate_tool_errors
def grok_plugin_call(
    server: str,
    tool: str,
    arguments_json: str = "{}",
    bot: str | None = None,
    confirmed: mcp_support.Confirmation = False,
) -> str:
    """Execute a connected plugin tool only after explicit confirmation. Set confirmed=true only when the user requested this exact server/tool/arguments operation."""
    if not mcp_support.require_confirmation(confirmed):
        return mcp_support.CONFIRMATION_REQUIRED
    from .plugin_tools import parse_arguments_json, resolve_catalog_tool_name

    try:
        arguments = parse_arguments_json(arguments_json)
    except (TypeError, ValueError) as exc:
        return f"Plugin execution blocked: {exc}."
    manager = GatewayManager()
    client = SandClient()
    try:
        canonical_name = resolve_catalog_tool_name(
            client.list_sand_mcp_tools([server]),
            tool,
            server,
        )
    except (TypeError, ValueError) as exc:
        return f"Plugin execution blocked: {exc}."
    result: object = client.execute_sand_mcp_tool(
        server_identifier=server,
        tool_name=canonical_name,
        arguments=arguments,
        tool_call_id=str(uuid.uuid4()),
        agent_id=_resolve(manager, bot),
    )
    return json.dumps(result, ensure_ascii=False, indent=2)


@_translate_tool_errors
def grok_swarm_send(
    text: str,
    bots: list[str] | None = None,
    exclude: list[str] | None = None,
    timeout_s: float = 600,
    rounds: int = 1,
) -> str:
    """Ask existing Bots concurrently, optionally relay peer data, and return ordered final sections."""
    from .swarm import SwarmRequest, render, run_swarm

    outcome = run_swarm(
        GatewayManager(),
        SwarmRequest(bots, text, exclude or (), timeout_s, rounds),
        SubprocessRoundExecutor(),
    )
    return render(outcome)


@_translate_tool_errors
def grok_bot_tail(bot: str | None = None, limit: int = 15, full: bool = False) -> str:
    """Read structured recent transcript entries. Defaults to the dedicated groken Bot."""
    gateway = cast("Gateway", GatewayManager())
    raw_entries = gateway.transcript_tail(_resolve(gateway, bot))
    if not isinstance(raw_entries, list):
        raise TypeError("transcript response is not a list")
    entries = cast("list[object]", raw_entries)[-limit:] if limit else []
    result: list[JsonObject] = []
    for raw_entry in entries:
        entry = _object_dict(raw_entry)
        if entry is None:
            raise TypeError("transcript response contains a non-object")
        message = _object_dict(entry.get("message"))
        content = (
            message.get("content") if message is not None else entry.get("content")
        ) or ""
        if not full:
            content = str(content)[:300]
        result.append(
            {
                "id": entry.get("id"),
                "kind": entry.get("kind"),
                "timestampMs": entry.get("timestampMs"),
                "content": content,
            }
        )
    return json.dumps(result, ensure_ascii=False)


for fn in (
    grok_bot_list,
    grok_bot_add,
    grok_bot_duplicate,
    grok_team_create,
    grok_team_members,
    grok_team_ask,
    grok_bot_send,
    grok_bot_ask,
    grok_bot_status,
    grok_bot_capabilities,
    grok_bot_tail,
    grok_swarm_send,
    grok_plugin_list,
    grok_plugin_call,
    *OPERATION_TOOLS,
):
    server.add_tool(fn)


def main() -> None:
    import argparse

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    class Arguments(argparse.Namespace):
        transport: str = "stdio"
        host: str = "127.0.0.1"
        port: int = 8321

    parser = argparse.ArgumentParser(prog="groken-mcp")
    _ = parser.add_argument(
        "--transport", choices=["stdio", "sse", "http"], default="stdio"
    )
    _ = parser.add_argument("--host", default="127.0.0.1")
    _ = parser.add_argument("--port", type=int, default=8321)
    args = Arguments()
    _ = parser.parse_args(namespace=args)
    if args.transport == "stdio":
        asyncio.run(server.run_stdio_async())
    elif args.transport == "sse":
        asyncio.run(server.run_sse_async(host=args.host, port=args.port))
    else:
        asyncio.run(server.run_streamable_http_async(host=args.host, port=args.port))


if __name__ == "__main__":
    main()
