from __future__ import annotations

import json
from collections.abc import Mapping
from typing import cast


def _object_dict(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    return cast("dict[str, object]", value)


def _object_list(value: object) -> list[object] | None:
    if not isinstance(value, list):
        return None
    return cast("list[object]", value)


def parse_arguments_json(raw: str) -> dict[str, object]:
    try:
        value = cast("object", json.loads(raw))
    except (TypeError, ValueError) as exc:
        raise ValueError("tool arguments must be valid JSON") from exc
    arguments = _object_dict(value)
    if arguments is None:
        raise TypeError("tool arguments must be a JSON object")
    return arguments


def resolve_catalog_tool_name(
    payload: Mapping[str, object],
    requested: str,
    server_identifier: str | None = None,
) -> str:
    raw_servers = _object_list(payload.get("servers"))
    if raw_servers is None:
        raise TypeError("plugin catalog has no server list")
    short_match: str | None = None
    for raw_server in raw_servers:
        server = _object_dict(raw_server)
        if server is None:
            continue
        if server_identifier is not None and server.get("serverIdentifier") != server_identifier:
            continue
        raw_tools = _object_list(server.get("tools"))
        if raw_tools is None:
            continue
        for raw_tool in raw_tools:
            tool = _object_dict(raw_tool)
            if tool is None:
                continue
            name = tool.get("name")
            tool_name = tool.get("toolName")
            if name == requested and isinstance(name, str):
                return name
            if tool_name == requested and isinstance(name, str):
                short_match = name
    if short_match is not None:
        return short_match
    raise ValueError(f"plugin tool not found: {requested}")


def render_tool_catalog(payload: dict[str, object]) -> str:
    raw_servers = _object_list(payload.get("servers"))
    if not raw_servers:
        return "No plugin tools found."
    lines: list[str] = []
    for raw_server in raw_servers:
        server = _object_dict(raw_server)
        if server is None:
            continue
        identifier = str(server.get("serverIdentifier") or "?")
        status = str(server.get("status") or "unknown")
        account = str(server.get("accountLabel") or "default")
        tools = _object_list(server.get("tools")) or []
        lines.append(f"{identifier} [{status}] account={account} tools={len(tools)}")
        for raw_tool in tools:
            tool = _object_dict(raw_tool)
            if tool is None:
                continue
            name = str(tool.get("name") or tool.get("toolName") or "?")
            description = str(tool.get("description") or "").replace("\n", " ")[:180]
            lines.append(f"  {name}" + (f" - {description}" if description else ""))
    return "\n".join(lines) if lines else "No plugin tools found."
