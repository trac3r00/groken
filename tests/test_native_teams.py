from __future__ import annotations

import json
import sys
from typing import TypeAlias, cast

import pytest

from groken import cli
from groken.native_teams import (
    MAX_TEAM_MEMBERS,
    NativeTeamError,
    ask_native_team,
    create_native_team,
    get_native_team,
)

JsonValue: TypeAlias = (
    None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]
)


class TeamManager:
    def __init__(self) -> None:
        self.roster: list[JsonValue] = [
            {"id": "lead-id", "name": "groken", "isGroup": False},
            {"id": "research-id", "name": "researcher", "isGroup": False},
            {"id": "code-id", "name": "coder", "isGroup": False},
            {
                "id": "delivery-id",
                "name": "delivery",
                "isGroup": True,
                "memberIds": ["code-id", "research-id"],
            },
        ]
        self.commands: list[tuple[str, dict[str, JsonValue] | None]] = []
        self.once: list[tuple[str, dict[str, JsonValue]]] = []
        self.asks: list[tuple[str, str, float]] = []

    def command(
        self, method: str, args: dict[str, JsonValue] | None = None
    ) -> JsonValue:
        self.commands.append((method, args))
        assert method == "listAgents"
        return list(self.roster)

    def command_once(self, method: str, args: dict[str, JsonValue]) -> JsonValue:
        self.once.append((method, args))
        assert method == "createGroup"
        return {
            "agent": {
                "id": "new-team-id",
                "name": args["name"],
                "isGroup": True,
                "memberIds": args["memberAgentIds"],
            }
        }

    def ask(self, agent_id: str, text: str, timeout_s: float = 600) -> str:
        self.asks.append((agent_id, text, timeout_s))
        return "native team answer"


def test_create_native_team_resolves_members_and_uses_create_group_once() -> None:
    manager = TeamManager()

    team = create_native_team(
        manager, "launch", ["researcher", "code-id"], "Ship safely"
    )

    assert team.team_id == "new-team-id"
    assert team.name == "launch"
    assert [(member.agent_id, member.name) for member in team.members] == [
        ("research-id", "researcher"),
        ("code-id", "coder"),
    ]
    assert manager.once == [
        (
            "createGroup",
            {
                "name": "launch",
                "description": "Ship safely",
                "memberAgentIds": ["research-id", "code-id"],
            },
        )
    ]


def test_native_team_members_preserve_group_order() -> None:
    team = get_native_team(TeamManager(), "delivery")

    assert team.team_id == "delivery-id"
    assert [(member.agent_id, member.name) for member in team.members] == [
        ("code-id", "coder"),
        ("research-id", "researcher"),
    ]


def test_native_team_ask_sends_one_prompt_to_group_agent() -> None:
    manager = TeamManager()

    answer = ask_native_team(manager, "delivery", "prepare release", 45)

    assert answer == "native team answer"
    assert manager.asks == [("delivery-id", "prepare release", 45)]


@pytest.mark.parametrize(
    ("members", "match"),
    [
        (["researcher"], "at least 2"),
        (["researcher", "researcher"], "duplicate"),
        (["delivery", "coder"], "cannot contain a team"),
        ([f"bot-{index}" for index in range(MAX_TEAM_MEMBERS + 1)], "at most"),
    ],
)
def test_invalid_native_team_members_are_rejected_before_creation(
    members: list[str], match: str
) -> None:
    manager = TeamManager()
    for index in range(MAX_TEAM_MEMBERS + 1):
        manager.roster.append(
            {"id": f"bot-{index}-id", "name": f"bot-{index}", "isGroup": False}
        )

    with pytest.raises(NativeTeamError, match=match):
        _ = create_native_team(manager, "invalid", members)

    assert manager.once == []


def test_cli_native_team_create_members_and_ask(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    manager = TeamManager()
    monkeypatch.setattr(cli, "_manager", lambda: manager)

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "groken",
            "team",
            "create",
            "launch",
            "--bots",
            "researcher,coder",
            "--description",
            "Ship safely",
        ],
    )
    cli.main()
    assert "Created native team launch (new-team-id)" in capsys.readouterr().out

    monkeypatch.setattr(sys, "argv", ["groken", "team", "members", "delivery"])
    cli.main()
    members_out = capsys.readouterr().out
    assert members_out.index("coder") < members_out.index("researcher")

    monkeypatch.setattr(
        sys,
        "argv",
        ["groken", "team", "ask", "delivery", "prepare release", "--timeout", "45"],
    )
    cli.main()
    assert capsys.readouterr().out.strip() == "native team answer"


@pytest.mark.anyio
async def test_mcp_native_team_tools_use_confirmation_and_group_ask(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import groken.mcp_server as m

    manager = TeamManager()
    monkeypatch.setattr(m, "GatewayManager", lambda: manager)

    assert m.grok_team_create("launch", ["researcher", "coder"]) == (
        "Operation blocked: review the exact operation with the user, then retry with "
        "confirmed=true."
    )
    created = json.loads(
        cast(
            "str",
            m.grok_team_create(
                "launch", ["researcher", "coder"], "Ship safely", confirmed=True
            ),
        )
    )
    assert created["id"] == "new-team-id"
    members = json.loads(cast("str", m.grok_team_members("delivery")))
    assert [member["name"] for member in members["members"]] == [
        "coder",
        "researcher",
    ]
    answer = await m.grok_team_ask("delivery", "prepare release", timeout_s=45)
    assert answer == "native team answer"
    assert manager.asks == [("delivery-id", "prepare release", 45)]


@pytest.mark.anyio
async def test_mcp_native_team_tools_are_registered() -> None:
    import groken.mcp_server as m

    tools = {tool.name: tool for tool in await m.server.list_tools()}

    assert {"grok_team_create", "grok_team_members", "grok_team_ask"} <= set(tools)
    assert "confirmed" in tools["grok_team_create"].input_schema.get("properties", {})
