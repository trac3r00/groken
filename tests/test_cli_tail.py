import json

import groken.cli as cli
import groken.mcp_server as mcp


ENTRIES = [
    {"id": "one", "kind": "user", "timestampMs": 100, "content": "a"},
    {"id": "two", "kind": "assistant", "timestampMs": 200, "content": "b" * 200},
    {"id": "three", "kind": "user", "timestampMs": 300, "content": "c"},
]


class FakeManager:
    def resolve_agent(self, agent):
        return agent or "default"

    def transcript_tail(self, agent):
        return ENTRIES


def test_tail_human_flags_and_since(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_manager", lambda: FakeManager())
    cli.cmd_tail(None, limit=1, since="one", full=True)
    assert capsys.readouterr().out == "[300] [user] c\n"


def test_tail_json_is_structured_and_valid(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_manager", lambda: FakeManager())
    cli.cmd_tail(None, limit=2, as_json=True)
    payload = json.loads(capsys.readouterr().out)
    assert [entry["id"] for entry in payload] == ["two", "three"]
    assert set(payload[0]) == {"id", "kind", "timestampMs", "content"}


def test_mcp_tail_limit_and_full(monkeypatch):
    monkeypatch.setattr(mcp, "GatewayManager", lambda: FakeManager())
    payload = json.loads(mcp.grok_bot_tail(limit=1, full=True))
    assert payload == [{"id": "three", "kind": "user", "timestampMs": 300, "content": "c"}]


def test_mcp_tail_default_cuts_content(monkeypatch):
    monkeypatch.setattr(mcp, "GatewayManager", lambda: FakeManager())
    payload = json.loads(mcp.grok_bot_tail(limit=2))
    assert len(payload[0]["content"]) == 200
    assert len(payload[1]["content"]) == 1
