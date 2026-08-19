import asyncio
import json

from mcp.server.mcpserver import MCPServer

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
    mgr.session().send_prompt(_resolve(mgr, bot), text)
    return "sent"


async def grok_bot_ask(text: str, bot: str | None = None, timeout_s: float = 600, idle_s: float = 45) -> str:
    """Send a message to a Grok Bot and return its reply text. Defaults to the dedicated groken Bot (auto-created on first use); pass bot (name or id) only to target a specific other Bot."""
    mgr = GatewayManager()
    agent_id = _resolve(mgr, bot)
    reply = await asyncio.to_thread(mgr.session().ask, agent_id, text, timeout_s, idle_s)
    return reply or "(no reply text received)"


def grok_bot_tail(bot: str | None = None) -> str:
    """Read the recent transcript entries of a Grok Bot conversation. Defaults to the dedicated groken Bot."""
    mgr = GatewayManager()
    lines = []
    for e in mgr.session().transcript_tail(_resolve(mgr, bot))[-15:]:
        msg = e.get("message") or {}
        lines.append(f'[{e.get("kind")}] {str(msg.get("content") or "")[:300]}')
    return "\n".join(lines)


for fn in (grok_bot_list, grok_bot_send, grok_bot_ask, grok_bot_tail):
    server.add_tool(fn)


def main() -> None:
    asyncio.run(server.run_stdio_async())


if __name__ == "__main__":
    main()
