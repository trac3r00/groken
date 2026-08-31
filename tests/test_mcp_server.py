from collections.abc import Callable
from typing import Never, Protocol, cast

import pytest

import groken.mcp_server as m


class _DecoratedTool(Protocol):
    __name__: str


class _AgentResolver(Protocol):
    def resolve_agent(self, bot: str | None = None) -> str: ...


def test_tool_names_pinned() -> None:
    tools: tuple[_DecoratedTool, ...] = (
        m.grok_bot_list,
        m.grok_bot_send,
        m.grok_bot_ask,
        m.grok_bot_status,
        m.grok_bot_capabilities,
        m.grok_bot_tail,
        m.grok_swarm_send,
        m.grok_plugin_list,
        m.grok_plugin_call,
    )
    names = sorted(fn.__name__ for fn in tools)
    assert names == [
        "grok_bot_ask",
        "grok_bot_capabilities",
        "grok_bot_list",
        "grok_bot_send",
        "grok_bot_status",
        "grok_bot_tail",
        "grok_plugin_call",
        "grok_plugin_list",
        "grok_swarm_send",
    ]
    assert not any(name.startswith(("direct", "exec", "vnc")) for name in names)


def test_missing_login_is_returned_as_tool_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing_login() -> Never:
        raise SystemExit("No tokens. Run: groken login")

    monkeypatch.setattr(m, "GatewayManager", missing_login)

    assert m.grok_bot_list() == "grok_bot_list failed: local configuration error."


@pytest.mark.anyio
async def test_status_tool_is_registered() -> None:
    names = {tool.name for tool in await m.server.list_tools()}
    assert "grok_bot_status" in names


def test_plugin_call_requires_explicit_confirmation() -> None:
    assert "confirmed=true" in m.grok_plugin_call(
        "user-X", "search", "{}", confirmed=False
    )


def test_resolve_delegates_to_manager() -> None:
    class FakeMgr:
        def resolve_agent(self, bot: str | None = None) -> str:
            return f"resolved:{bot}"

    resolve_name = "_resolve"
    resolve = cast(
        "Callable[[_AgentResolver, str | None], str]",
        getattr(m, resolve_name),
    )
    assert resolve(FakeMgr(), "알림이") == "resolved:알림이"
    assert resolve(FakeMgr(), None) == "resolved:None"
