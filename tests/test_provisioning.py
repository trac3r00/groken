from pathlib import Path

import pytest

import groken.gateway as gw_mod
from groken.gateway import GatewayManager

CommandArgs = dict[str, object] | None
CommandCall = tuple[str, CommandArgs]


def fake_manager(
    agents: list[dict[str, object]], created: dict[str, object] | None = None
) -> tuple[GatewayManager, list[CommandCall]]:
    mgr = GatewayManager.__new__(GatewayManager)
    calls: list[CommandCall] = []

    def command(method: str, args: CommandArgs = None) -> object:
        calls.append((method, args))
        return agents if method == "listAgents" else created

    mgr.command = command
    return mgr, calls


def test_resolve_agent_by_name_and_id() -> None:
    mgr, _ = fake_manager([{"id": "u1", "name": "알림이"}, {"id": "u2", "name": "Zero"}])
    assert mgr.resolve_agent("알림이") == "u1"
    assert mgr.resolve_agent("u2") == "u2"


def test_resolve_agent_unknown_raises() -> None:
    mgr, _ = fake_manager([])
    try:
        _ = mgr.resolve_agent("nobody")
    except ValueError as e:
        assert "nobody" in str(e)
    else:
        raise AssertionError("expected ValueError")


def test_own_agent_uses_valid_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ = tmp_path

    def cached_bot_id() -> str:
        return "u1"

    monkeypatch.setattr(gw_mod, "cached_bot_id", cached_bot_id)
    monkeypatch.setattr(gw_mod, "bot_name", lambda: "groken")
    mgr, calls = fake_manager([{"id": "u1", "name": "groken"}])
    assert mgr.own_agent_id() == "u1"
    assert all(m == "listAgents" for m, _ in calls)


def test_own_agent_finds_existing_by_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ = tmp_path
    remembered: list[tuple[str, str]] = []

    def cached_bot_id() -> None:
        return None

    def bot_name() -> str:
        return "groken"

    def remember_bot(identifier: str, name: str) -> None:
        remembered.append((identifier, name))

    monkeypatch.setattr(gw_mod, "cached_bot_id", cached_bot_id)
    monkeypatch.setattr(gw_mod, "bot_name", bot_name)
    monkeypatch.setattr(gw_mod, "remember_bot", remember_bot)
    mgr, calls = fake_manager([{"id": "u9", "name": "groken"}])
    assert mgr.own_agent_id() == "u9"
    assert remembered == [("u9", "groken")]
    assert not any(m == "createAgent" for m, _ in calls)


def test_own_agent_creates_when_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ = tmp_path
    remembered: list[tuple[str, str]] = []

    def cached_bot_id() -> None:
        return None

    def bot_name() -> str:
        return "groken"

    def remember_bot(identifier: str, name: str) -> None:
        remembered.append((identifier, name))

    monkeypatch.setattr(gw_mod, "cached_bot_id", cached_bot_id)
    monkeypatch.setattr(gw_mod, "bot_name", bot_name)
    monkeypatch.setattr(gw_mod, "remember_bot", remember_bot)
    created: dict[str, object] = {"agent": {"id": "new-1"}}
    mgr, calls = fake_manager([], created=created)
    assert mgr.own_agent_id() == "new-1"
    create = next(a for m, a in calls if m == "createAgent")
    if create is None:
        raise AssertionError("createAgent call must include arguments")
    assert create["name"] == "groken"
    assert create["clientNonce"]
    assert remembered == [("new-1", "groken")]


def test_own_agent_recreates_when_cached_id_gone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ = tmp_path

    def cached_bot_id() -> str:
        return "stale-id"

    def bot_name() -> str:
        return "groken"

    def remember_bot(_identifier: str, _name: str) -> None:
        return None

    monkeypatch.setattr(gw_mod, "cached_bot_id", cached_bot_id)
    monkeypatch.setattr(gw_mod, "bot_name", bot_name)
    monkeypatch.setattr(gw_mod, "remember_bot", remember_bot)
    mgr, _ = fake_manager([{"id": "u7", "name": "groken"}])
    assert mgr.own_agent_id() == "u7"
