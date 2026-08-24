import asyncio
import json

from mcp.server.mcpserver import MCPServer

from .capabilities import capability_manifest, live_read_only_status
from .gateway import GatewayManager

server = MCPServer("groken")


def _resolve(mgr: GatewayManager, bot: str | None) -> str:
    return mgr.resolve_agent(bot)


def grok_bot_list() -> str:
    """List the user's Grok Bots with id, name, and running state."""
    agents = GatewayManager().command("listAgents")
    body = [{"id": a.get("id"), "name": a.get("name"), "running": a.get("isRunning")} for a in agents]
    return json.dumps(body, ensure_ascii=False, indent=2)


def grok_bot_send(text: str, bot: str | None = None) -> str:
    """Send a message to a Grok Bot without waiting for the reply. Defaults to the dedicated groken Bot (auto-created on first use); pass bot (name or id) only to target a specific other Bot."""
    mgr = GatewayManager()
    mgr.send_prompt(_resolve(mgr, bot), text)
    return "sent"


async def grok_bot_ask(text: str, bot: str | None = None, timeout_s: float = 600, idle_s: float = 45) -> str:
    """Send a message to a Grok Bot and return its reply text. Defaults to the dedicated groken Bot (auto-created on first use); pass bot (name or id) only to target a specific other Bot."""
    mgr = GatewayManager()
    agent_id = _resolve(mgr, bot)
    reply = await asyncio.to_thread(mgr.ask, agent_id, text, timeout_s, idle_s)
    return reply or "(no reply text received)"


def grok_bot_capabilities(include_commands: bool = False) -> str:
    """Inspect the official app's typed gateway surface plus safe live feature gates."""
    manager = GatewayManager()
    payload = capability_manifest(include_commands=include_commands)
    payload["live"] = live_read_only_status(manager)
    return json.dumps(payload, ensure_ascii=False, indent=2)


def grok_bot_tail(bot: str | None = None, limit: int = 15, full: bool = False) -> str:
    """Read structured recent transcript entries. Defaults to the dedicated groken Bot."""
    mgr = GatewayManager()
    entries = mgr.transcript_tail(_resolve(mgr, bot))[-limit:] if limit else []
    result = []
    for e in entries:
        content = e.get("content") or ""
        if not full:
            content = str(content)[:300]
        result.append({"id": e.get("id"), "kind": e.get("kind"), "timestampMs": e.get("timestampMs"), "content": content})
    return json.dumps(result, ensure_ascii=False)


for fn in (
    grok_bot_list,
    grok_bot_send,
    grok_bot_ask,
    grok_bot_capabilities,
    grok_bot_tail,
):
    server.add_tool(fn)


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(prog="groken-mcp")
    parser.add_argument("--transport", choices=["stdio", "sse", "http"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8321)
    args = parser.parse_args()
    if args.transport == "stdio":
        asyncio.run(server.run_stdio_async())
    elif args.transport == "sse":
        asyncio.run(server.run_sse_async(host=args.host, port=args.port))
    else:
        asyncio.run(server.run_streamable_http_async(host=args.host, port=args.port))


if __name__ == "__main__":
    main()
