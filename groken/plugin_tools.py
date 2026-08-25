from __future__ import annotations

import json
from collections.abc import Mapping
from typing import cast


def parse_arguments_json(raw: str) -> dict[str, object]:
    try:
        value = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("tool arguments must be valid JSON") from exc
    if not isinstance(value, dict):
        raise TypeError("tool arguments must be a JSON object")
    return cast("dict[str, object]", value)


def resolve_catalog_tool_name(
    payload: Mapping[str, object],
    requested: str,
    server_identifier: str | None = None,
) -> str:
    raw_servers = payload.get("servers")
    if not isinstance(raw_servers, list):
        raise TypeError("plugin catalog has no server list")
    short_match: str | None = None
    for raw_server in raw_servers:
        if not isinstance(raw_server, dict):
            continue
        if server_identifier is not None and raw_server.get("serverIdentifier") != server_identifier:
            continue
        raw_tools = raw_server.get("tools")
        if not isinstance(raw_tools, list):
            continue
        for raw_tool in raw_tools:
            if not isinstance(raw_tool, dict):
                continue
            name = raw_tool.get("name")
            tool_name = raw_tool.get("toolName")
            if name == requested and isinstance(name, str):
                return name
            if tool_name == requested and isinstance(name, str):
                short_match = name
    if short_match is not None:
        return short_match
    raise ValueError(f"plugin tool not found: {requested}")


def render_tool_catalog(payload: dict[str, object]) -> str:
    raw_servers = payload.get("servers")
    if not isinstance(raw_servers, list) or not raw_servers:
        return "No plugin tools found."
    lines: list[str] = []
    for raw_server in raw_servers:
        if not isinstance(raw_server, dict):
            continue
        server = cast("dict[str, object]", raw_server)
        identifier = str(server.get("serverIdentifier") or "?")
        status = str(server.get("status") or "unknown")
        account = str(server.get("accountLabel") or "default")
        raw_tools = server.get("tools")
        tools = raw_tools if isinstance(raw_tools, list) else []
        lines.append(f"{identifier} [{status}] account={account} tools={len(tools)}")
        for raw_tool in tools:
            if not isinstance(raw_tool, dict):
                continue
            tool = cast("dict[str, object]", raw_tool)
            name = str(tool.get("name") or tool.get("toolName") or "?")
            description = str(tool.get("description") or "").replace("\n", " ")[:180]
            lines.append(f"  {name}" + (f" - {description}" if description else ""))
    return "\n".join(lines) if lines else "No plugin tools found."
