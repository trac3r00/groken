import groken.gateway as gw_mod
from groken.gateway import GatewayManager


def fake_manager(agents, created=None):
    mgr = GatewayManager.__new__(GatewayManager)
    calls = []
    mgr.command = lambda method, args=None: calls.append((method, args)) or (
        agents if method == "listAgents" else created
    )
    return mgr, calls


def test_resolve_agent_by_name_and_id():
    mgr, _ = fake_manager([{"id": "u1", "name": "알림이"}, {"id": "u2", "name": "Zero"}])
    assert mgr.resolve_agent("알림이") == "u1"
    assert mgr.resolve_agent("u2") == "u2"


def test_resolve_agent_unknown_raises():
    mgr, _ = fake_manager([])
    try:
        mgr.resolve_agent("nobody")
    except ValueError as e:
        assert "nobody" in str(e)
    else:
        raise AssertionError("expected ValueError")


def test_own_agent_uses_valid_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(gw_mod, "cached_bot_id", lambda: "u1")
    mgr, calls = fake_manager([{"id": "u1", "name": "groken"}])
    assert mgr.own_agent_id() == "u1"
    assert all(m == "listAgents" for m, _ in calls)


def test_own_agent_finds_existing_by_name(tmp_path, monkeypatch):
    remembered = []
    monkeypatch.setattr(gw_mod, "cached_bot_id", lambda: None)
    monkeypatch.setattr(gw_mod, "bot_name", lambda: "groken")
    monkeypatch.setattr(gw_mod, "remember_bot", lambda i, n: remembered.append((i, n)))
    mgr, calls = fake_manager([{"id": "u9", "name": "groken"}])
    assert mgr.own_agent_id() == "u9"
    assert remembered == [("u9", "groken")]
    assert not any(m == "createAgent" for m, _ in calls)


def test_own_agent_creates_when_missing(tmp_path, monkeypatch):
    remembered = []
    monkeypatch.setattr(gw_mod, "cached_bot_id", lambda: None)
    monkeypatch.setattr(gw_mod, "bot_name", lambda: "groken")
    monkeypatch.setattr(gw_mod, "remember_bot", lambda i, n: remembered.append((i, n)))
    created = {"agent": {"id": "new-1"}}
    mgr, calls = fake_manager([], created=created)
    assert mgr.own_agent_id() == "new-1"
    create = next(a for m, a in calls if m == "createAgent")
    assert create["name"] == "groken"
    assert create["clientNonce"]
    assert remembered == [("new-1", "groken")]


def test_own_agent_recreates_when_cached_id_gone(tmp_path, monkeypatch):
    monkeypatch.setattr(gw_mod, "cached_bot_id", lambda: "stale-id")
    monkeypatch.setattr(gw_mod, "bot_name", lambda: "groken")
    monkeypatch.setattr(gw_mod, "remember_bot", lambda i, n: None)
    mgr, _ = fake_manager([{"id": "u7", "name": "groken"}])
    assert mgr.own_agent_id() == "u7"
