import groken.gateway as gw_mod
from groken.gateway import GatewayManager
from groken.provisioning import WORKER_DESCRIPTION


def _mgr(agents, created=None):
    mgr = GatewayManager.__new__(GatewayManager)
    calls = []
    mgr.command = lambda method, args=None: calls.append((method, args)) or (
        agents if method == "listAgents" else created
    )
    return mgr, calls


def test_worker_description_carries_guardrails():
    for needle in ("verify", "post-action", "never delete", "ask before destructive"):
        assert needle in WORKER_DESCRIPTION.lower(), f"missing guardrail: {needle}"


def test_cached_bot_must_match_configured_name(monkeypatch):
    agents = [
        {"id": "top-id", "name": "top-bot", "description": WORKER_DESCRIPTION},
        {"id": "groken-id", "name": "groken", "description": WORKER_DESCRIPTION},
    ]
    mgr, _calls = _mgr(agents)
    remembered: list[tuple[str, str]] = []
    monkeypatch.setattr(gw_mod, "cached_bot_id", lambda: "top-id")
    monkeypatch.setattr(gw_mod, "bot_name", lambda: "groken")
    monkeypatch.setattr(gw_mod, "remember_bot", lambda i, n: remembered.append((i, n)))

    assert mgr.own_agent_id() == "groken-id"
    assert remembered == [("groken-id", "groken")]


def test_existing_own_bot_gets_description_upgraded(monkeypatch):
    mgr, calls = _mgr([{"id": "u9", "name": "groken", "description": "old"}])
    monkeypatch.setattr(gw_mod, "cached_bot_id", lambda: None)
    monkeypatch.setattr(gw_mod, "bot_name", lambda: "groken")
    monkeypatch.setattr(gw_mod, "remember_bot", lambda i, n: None)
    mgr.own_agent_id()
    updates = [a for m, a in calls if m == "updateAgent"]
    assert updates, "expected updateAgent call to refresh description on existing bot"
    assert updates[0]["id"] == "u9"
    assert "never delete" in updates[0]["description"].lower()
