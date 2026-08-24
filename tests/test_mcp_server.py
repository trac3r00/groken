import groken.mcp_server as m


def test_tool_names_pinned():
    names = sorted(fn.__name__ for fn in (m.grok_bot_list, m.grok_bot_send, m.grok_bot_ask, m.grok_bot_tail))
    assert names == ["grok_bot_ask", "grok_bot_list", "grok_bot_send", "grok_bot_tail"]
    assert not any(name.startswith(("direct", "exec", "vnc")) for name in names)


def test_resolve_delegates_to_manager():
    class FakeMgr:
        def resolve_agent(self, bot):
            return f"resolved:{bot}"

    assert m._resolve(FakeMgr(), "알림이") == "resolved:알림이"
    assert m._resolve(FakeMgr(), None) == "resolved:None"
