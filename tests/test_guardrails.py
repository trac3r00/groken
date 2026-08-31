from collections.abc import Mapping, Sequence
from typing import Protocol, TypedDict, cast, final

import pytest

import groken.gateway as gw_mod
from groken.gateway import GatewayManager
from groken.provisioning import WORKER_DESCRIPTION

JsonObject = Mapping[str, object]
Call = tuple[str, dict[str, object] | None]


class AgentProfile(TypedDict):
    name: str
    description: str


class UpdateAgentArgs(TypedDict):
    id: str
    profile: AgentProfile


class OwnAgentManager(Protocol):
    def command(self, method: str, args: dict[str, object] | None = None) -> object: ...

    def own_agent_id(self) -> str: ...


@final
class FakeManager:
    def __init__(
        self,
        agents: Sequence[JsonObject],
        created: JsonObject | None = None,
    ) -> None:
        self.agents: Sequence[JsonObject] = agents
        self.created: JsonObject | None = created
        self.calls: list[Call] = []

    def command(self, method: str, args: dict[str, object] | None = None) -> object:
        self.calls.append((method, args))
        return self.agents if method == "listAgents" else self.created

    def own_agent_id(self) -> str:
        return GatewayManager.own_agent_id(cast(GatewayManager, cast(object, self)))


def _mgr(
    agents: Sequence[JsonObject], created: JsonObject | None = None
) -> tuple[OwnAgentManager, list[Call]]:
    manager = FakeManager(agents, created)
    return manager, manager.calls


def test_worker_description_carries_guardrails() -> None:
    for needle in ("verify", "post-action", "never delete", "ask before destructive"):
        assert needle in WORKER_DESCRIPTION.lower(), f"missing guardrail: {needle}"


def test_cached_bot_must_match_configured_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agents = [
        {"id": "top-id", "name": "top-bot", "description": WORKER_DESCRIPTION},
        {"id": "groken-id", "name": "groken", "description": WORKER_DESCRIPTION},
    ]
    mgr, _calls = _mgr(agents)
    remembered: list[tuple[str, str]] = []
    monkeypatch.setattr(gw_mod, "cached_bot_id", lambda: "top-id")
    monkeypatch.setattr(gw_mod, "bot_name", lambda: "groken")

    def remember(identifier: str, name: str) -> None:
        remembered.append((identifier, name))

    monkeypatch.setattr(gw_mod, "remember_bot", remember)

    assert mgr.own_agent_id() == "groken-id"
    assert remembered == [("groken-id", "groken")]


def test_existing_own_bot_gets_description_upgraded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mgr, calls = _mgr([{"id": "u9", "name": "groken", "description": "old"}])
    monkeypatch.setattr(gw_mod, "cached_bot_id", lambda: None)
    monkeypatch.setattr(gw_mod, "bot_name", lambda: "groken")

    def remember(_identifier: str, _name: str) -> None:
        return None

    monkeypatch.setattr(gw_mod, "remember_bot", remember)
    _ = mgr.own_agent_id()
    updates = [
        cast(UpdateAgentArgs, cast(object, args))
        for method, args in calls
        if method == "updateAgent" and args is not None
    ]
    assert updates, "expected updateAgent call to refresh description on existing bot"
    assert updates[0]["id"] == "u9"
    assert updates[0]["profile"]["name"] == "groken"
    assert "never delete" in updates[0]["profile"]["description"].lower()
