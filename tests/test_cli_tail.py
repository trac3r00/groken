import json
from typing import cast

import pytest

import groken.mcp_server as mcp
from groken import cli

ENTRIES: list[dict[str, object]] = [
    {
        "id": "one",
        "kind": "send-message",
        "timestampMs": 100,
        "message": {"type": "text", "content": "a"},
    },
    {
        "id": "two",
        "kind": "send-message",
        "timestampMs": 200,
        "message": {"type": "text", "content": "b" * 200},
    },
    {
        "id": "three",
        "kind": "send-message",
        "timestampMs": 300,
        "message": {"type": "text", "content": "c"},
    },
]


class FakeManager:
    def resolve_agent(self, agent: str | None) -> str:
        return agent or "default"

    def transcript_tail(self, agent: str) -> list[dict[str, object]]:
        _ = agent
        return ENTRIES


def _decode_entries(payload: str) -> list[dict[str, object]]:
    return cast("list[dict[str, object]]", json.loads(payload))


def test_tail_human_flags_and_since(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "_manager", lambda: FakeManager())
    cli.cmd_tail(None, limit=1, since="one", full=True)
    assert capsys.readouterr().out == "[300] [send-message] c\n"


def test_tail_json_is_structured_and_valid(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "_manager", lambda: FakeManager())
    cli.cmd_tail(None, limit=2, as_json=True)
    payload = _decode_entries(capsys.readouterr().out)
    assert [entry["id"] for entry in payload] == ["two", "three"]
    assert set(payload[0]) == {"id", "kind", "timestampMs", "content"}


def test_mcp_tail_limit_and_full(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp, "GatewayManager", lambda: FakeManager())
    payload = _decode_entries(mcp.grok_bot_tail(limit=1, full=True))
    assert payload == [
        {
            "id": "three",
            "kind": "send-message",
            "timestampMs": 300,
            "content": "c",
        }
    ]


def test_mcp_tail_default_cuts_content(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp, "GatewayManager", lambda: FakeManager())
    payload = _decode_entries(mcp.grok_bot_tail(limit=2))
    first_content = payload[0]["content"]
    second_content = payload[1]["content"]
    assert isinstance(first_content, str)
    assert isinstance(second_content, str)
    assert len(first_content) == 200
    assert len(second_content) == 1
