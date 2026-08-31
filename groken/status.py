from __future__ import annotations

from typing import Protocol, cast

from .local_health import collect_local_status
from .vnc import display_from_forever_box


class StatusGateway(Protocol):
    def own_agent_id(self) -> str: ...
    def command(self, method: str, args: dict[str, object] | None = None) -> object: ...


def _record(value: object) -> dict[str, object]:
    return cast("dict[str, object]", value) if isinstance(value, dict) else {}


def _objects(value: object) -> list[object]:
    return cast("list[object]", value) if isinstance(value, list) else []


def _integer(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def collect_status(gateway: StatusGateway) -> dict[str, object]:
    local = collect_local_status()
    bot_id = gateway.own_agent_id()
    computer = _record(gateway.command("getForeverBoxStatus", {"id": bot_id}))
    host = _record(gateway.command("getHostStatus"))
    secrets = _record(gateway.command("getBoxSecretsStatus"))
    mcp = _record(gateway.command("listBoxMcpServers", {"serverIdentifiers": []}))
    servers: list[dict[str, object]] = []
    for raw in _objects(mcp.get("servers")):
        row = _record(raw)
        servers.append(
            {
                "id": row.get("serverIdentifier"),
                "status": row.get("status"),
                "tool_count": _integer(row.get("toolCount")),
                "detail": row.get("statusDetail")
                if isinstance(row.get("statusDetail"), str)
                else None,
            }
        )
    return {
        "bot": {
            "id": bot_id,
            "state": computer.get("state"),
            "display": display_from_forever_box(computer),
        },
        "host": {
            "version": host.get("hostVersion"),
            "latest_version": host.get("latestHostVersion"),
            "update_available": host.get("hostUpdateAvailable") is True,
            "busy": host.get("isBusy") is True,
            "capabilities": host.get("capabilities")
            if isinstance(host.get("capabilities"), list)
            else [],
        },
        "secrets": {
            "is_applied": secrets.get("isApplied") is True,
            "last_applied_at_ms": _integer(secrets.get("lastAppliedAtMs")),
        },
        "mcp": {
            "connected": sum(server.get("status") == "connected" for server in servers),
            "errors": sum(server.get("status") == "error" for server in servers),
            "servers": servers,
        },
        "local": local,
    }


def render_status(status: dict[str, object]) -> str:
    bot = _record(status.get("bot"))
    host = _record(status.get("host"))
    secrets = _record(status.get("secrets"))
    mcp = _record(status.get("mcp"))
    display = bot.get("display")
    display_text = f", display :{display}" if isinstance(display, int) else ""
    host_state = (
        "update available" if host.get("update_available") is True else "current"
    )
    busy = ", busy" if host.get("busy") is True else ""
    lines = [
        f"Bot: {bot.get('id')} ({bot.get('state')}{display_text})",
        f"Host: {host.get('version')} ({host_state}{busy})",
        f"Secrets: applied={secrets.get('is_applied')}",
        f"MCP: {mcp.get('connected')} connected, {mcp.get('errors')} error",
    ]
    for raw in _objects(mcp.get("servers")):
        server = _record(raw)
        if server.get("status") != "error":
            continue
        detail = str(server.get("detail") or "unknown error").replace("\n", " ")[:240]
        lines.append(f"  {server.get('id')}: error - {detail}")
    local = _record(status.get("local"))
    if local:
        lines.extend(
            (
                f"Harnesses: {_record(local.get('harnesses')).get('message')}",
                f"Routines: {_record(local.get('routines')).get('message')}",
                f"Environment: {_record(local.get('environment')).get('message')}",
                f"Native: {_record(local.get('native')).get('message')}",
                f"Lifecycle/swarm: {local.get('lifecycle_swarm')}",
            )
        )
    return "\n".join(lines)
