import groken.mcp_server as m


def test_tool_names_pinned():
    names = sorted(fn.__name__ for fn in (
        m.grok_bot_list,
        m.grok_bot_send,
        m.grok_bot_ask,
        m.grok_bot_tail,
        m.grok_plugin_list,
        m.grok_plugin_call,
    ))
    assert names == [
        "grok_bot_ask",
        "grok_bot_list",
        "grok_bot_send",
        "grok_bot_tail",
        "grok_plugin_call",
        "grok_plugin_list",
    ]
    assert not any(name.startswith(("direct", "exec", "vnc")) for name in names)


def test_plugin_call_requires_explicit_confirmation():
    assert "confirmed=true" in m.grok_plugin_call("user-X", "search", "{}", confirmed=False)


def test_resolve_delegates_to_manager():
    class FakeMgr:
        def resolve_agent(self, bot):
            return f"resolved:{bot}"

    assert m._resolve(FakeMgr(), "알림이") == "resolved:알림이"
    assert m._resolve(FakeMgr(), None) == "resolved:None"
