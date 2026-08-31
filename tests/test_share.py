import os
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from groken.share_client import (
    RelayManager,
    ShareLink,
    clear_share_link,
    load_share_link,
    save_share_link,
)
from groken.share_server import create_share_app
from groken.share_store import ShareStore


class FakeFeed:
    def __init__(self, events: Iterator[dict[str, object]]) -> None:
        self.events = events

    def next_event(
        self, timeout_s: float | None, *, hold: bool = False
    ) -> dict[str, object]:
        del timeout_s, hold
        return next(self.events)

    def resume(self) -> None:
        return None


class FakeManager:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.resolve_calls = 0

    def close(self) -> None:
        return None

    def resolve_agent(self, bot: str | None = None) -> str:
        self.resolve_calls += 1
        raise AssertionError(f"mutable name resolution must not run: {bot}")

    def command(self, method: str, args: dict[str, object] | None = None) -> object:
        if method == "listAgents":
            return [
                {"id": "agent-shared", "name": "friend-bot"},
                {"id": "agent-private", "name": "private-bot"},
            ]
        return {"method": method, "args": args or {}}

    def send_prompt(self, agent_id: str, text: str) -> dict[str, object]:
        self.sent.append((agent_id, text))
        return {"accepted": True}

    def transcript_tail(self, agent_id: str) -> list[dict[str, object]]:
        assert agent_id == "agent-shared"
        return [{"id": "m1", "content": "hello"}]

    def ask(self, agent_id: str, text: str, timeout_s: float = 600) -> str:
        assert agent_id == "agent-shared"
        assert text == "question"
        assert timeout_s == 12
        return "answer"

    def ask_stream(
        self,
        agent_id: str,
        text: str,
        timeout_s: float = 600,
        idle_s: float = 45,
        on_chunk: Callable[[str], None] | None = None,
    ) -> str:
        assert agent_id == "agent-shared"
        if on_chunk is not None:
            on_chunk("an")
            on_chunk("swer")
        return "answer"

    def events(self, channels: list[str] | None = None) -> Iterator[dict[str, object]]:
        _ = channels
        yield {"event": "message", "data": {"agentId": "agent-shared"}}
        yield {"event": "message", "data": {"agentId": "agent-private"}}

    @contextmanager
    def event_subscription(
        self, channels: list[str], timeout_s: float | None
    ) -> Iterator[FakeFeed]:
        del timeout_s
        yield FakeFeed(self.events(channels))

    def ensure_sandbox_metadata(self) -> dict[str, object]:
        return {
            "execDaemonUrl": "https://exec.example.test",
            "networkToken": "network-token",
            "execDaemonAuthToken": "exec-token",
            "podId": "pod-1",
            "accountSecret": "must-not-leak",
        }


def test_share_store_creates_private_revocable_bot_grant(tmp_path: Path) -> None:
    path = tmp_path / "shares.json"
    store = ShareStore(path)

    grant = store.create("bob", "agent-shared", "friend-bot")

    assert grant.token.startswith("grk_share_")
    assert grant.record.name == "bob"
    assert grant.record.bot_name == "friend-bot"
    assert store.authenticate(grant.token) == grant.record
    assert grant.token not in path.read_text()
    assert os.stat(path).st_mode & 0o777 == 0o600

    revoked = store.revoke("bob")
    assert revoked.revoked is True
    assert store.authenticate(grant.token) is None


def test_share_store_uses_private_default_path(
    tmp_path: Path, monkeypatch: Any
) -> None:
    from groken import share_store

    path = tmp_path / "shares.json"
    monkeypatch.setattr(share_store, "_DEFAULT_PATH", path)

    grant = share_store.ShareStore().create("bob", "agent-shared", "friend-bot")

    assert grant.record.bot_name == "friend-bot"
    assert path.exists()


def test_share_server_pins_every_request_to_shared_bot(tmp_path: Path) -> None:
    manager = FakeManager()
    store = ShareStore(tmp_path / "shares.json")
    grant = store.create("bob", "agent-shared", "friend-bot")
    client = TestClient(create_share_app(lambda: manager, store))
    auth = {"authorization": f"Bearer {grant.token}"}

    assert client.get("/v1/bot").status_code == 401
    assert client.get("/v1/bot", headers=auth).json() == {
        "agent_id": "agent-shared",
        "name": "friend-bot",
    }

    sent = client.post("/v1/send", headers=auth, json={"text": "hello"})
    assert sent.status_code == 200
    assert sent.json() == {"accepted": True}
    assert manager.sent == [("agent-shared", "hello")]

    agents = client.post(
        "/v1/command", headers=auth, json={"method": "listAgents", "args": {}}
    )
    assert agents.json() == [{"id": "agent-shared", "name": "friend-bot"}]

    denied = client.post(
        "/v1/command", headers=auth, json={"method": "createAgent", "args": {}}
    )
    assert denied.status_code == 403

    asked = client.post(
        "/v1/ask",
        headers=auth,
        json={"text": "question", "timeout_s": 12},
    )
    assert asked.json() == {"reply": "answer"}

    assert client.get("/v1/transcript", headers=auth).json() == [
        {"id": "m1", "content": "hello"}
    ]
    assert client.get("/v1/metadata", headers=auth).status_code == 404

    streamed = client.post(
        "/v1/ask/stream",
        headers=auth,
        json={"text": "question", "timeout_s": 12},
    )
    assert streamed.status_code == 200
    assert 'event: chunk\ndata: {"text": "an"}' in streamed.text
    assert 'event: done\ndata: {"reply": "answer"}' in streamed.text

    events = client.get("/v1/events", headers=auth)
    assert events.status_code == 200
    assert "agent-shared" in events.text
    assert "agent-private" not in events.text
    assert manager.resolve_calls == 0


def test_bot_identity_does_not_require_live_gateway(tmp_path: Path) -> None:
    store = ShareStore(tmp_path / "shares.json")
    grant = store.create("bob", "agent-shared", "friend-bot")
    auth = {"authorization": f"Bearer {grant.token}"}

    def logged_out_factory() -> Any:
        raise SystemExit("No tokens. Run: groken login")

    client = TestClient(
        create_share_app(logged_out_factory, store),
        raise_server_exceptions=False,
    )

    assert client.get("/v1/bot", headers=auth).json() == {
        "agent_id": "agent-shared",
        "name": "friend-bot",
    }


def test_relay_manager_uses_only_share_endpoints() -> None:
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        if request.url.path == "/v1/bot":
            return httpx.Response(
                200, json={"agent_id": "agent-shared", "name": "friend-bot"}
            )
        if request.url.path == "/v1/send":
            return httpx.Response(200, json={"accepted": True})
        if request.url.path == "/v1/ask":
            return httpx.Response(200, json={"reply": "answer"})
        if request.url.path == "/v1/transcript":
            return httpx.Response(200, json=[{"id": "m1"}])
        return httpx.Response(404)

    http = httpx.Client(transport=httpx.MockTransport(handler))
    manager = RelayManager(ShareLink("https://relay.example.test", "secret"), http=http)

    assert manager.resolve_agent() == "agent-shared"
    assert manager.send_prompt("ignored", "hello") == {"accepted": True}
    assert manager.ask("ignored", "question") == "answer"
    assert manager.transcript_tail("ignored") == [{"id": "m1"}]
    assert ("POST", "/v1/send") in seen
    assert all(path.startswith("/v1/") for _, path in seen)


def test_relay_stream_returns_authoritative_final_reply() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/ask/stream"
        return httpx.Response(
            200,
            text=(
                'event: chunk\ndata: {"text": "partial"}\n\n'
                'event: done\ndata: {"reply": "complete answer"}\n\n'
            ),
        )

    http = httpx.Client(transport=httpx.MockTransport(handler))
    manager = RelayManager(ShareLink("https://relay.example.test", "secret"), http=http)
    chunks: list[str] = []

    reply = manager.ask_stream("ignored", "question", on_chunk=chunks.append)

    assert chunks == ["partial"]
    assert reply == "complete answer"


def test_share_cli_lifecycle_uses_private_default_paths(
    tmp_path: Path, monkeypatch: Any, capsys: Any
) -> None:
    from groken import cli, share_client, share_store

    shares_path = tmp_path / "shares.json"
    link_path = tmp_path / "share.json"
    monkeypatch.setattr(share_store, "_DEFAULT_PATH", shares_path)
    monkeypatch.setattr(share_client, "_DEFAULT_PATH", link_path)

    cli.cmd_share_create("bob", "friend-bot", manager=FakeManager())
    created = capsys.readouterr().out
    assert "Created share bob for friend-bot (agent-shared)" in created
    assert "grk_share_" in created

    cli.cmd_share_list()
    listed = capsys.readouterr().out
    assert "bob  friend-bot  agent-shared  active" in listed
    assert "grk_share_" not in listed

    token_path = tmp_path / "share-token"
    token_path.write_text("grk_share_secret\n")
    token_path.chmod(0o600)
    cli.cmd_share_connect("https://relay.example.test", token_file=str(token_path))
    connected = capsys.readouterr().out
    assert "https://relay.example.test" in connected
    assert "grk_share_secret" not in connected

    cli.cmd_share_status()
    status_output = capsys.readouterr().out
    assert "https://relay.example.test" in status_output
    assert "grk_share_secret" not in status_output

    cli.cmd_share_revoke("bob")
    assert "Revoked share: bob" in capsys.readouterr().out
    cli.cmd_share_disconnect()
    assert "Disconnected." in capsys.readouterr().out


@pytest.mark.parametrize("operation", ["create", "list", "revoke"])
def test_share_cli_translates_corrupt_store_to_actionable_exit(
    tmp_path: Path, monkeypatch: Any, operation: str
) -> None:
    from groken import cli, share_store

    shares_path = tmp_path / "shares.json"
    shares_path.write_text("{malformed")
    monkeypatch.setattr(share_store, "_DEFAULT_PATH", shares_path)

    with pytest.raises(SystemExit, match="repair or remove"):
        if operation == "create":
            cli.cmd_share_create("bob", "friend-bot", manager=FakeManager())
        elif operation == "list":
            cli.cmd_share_list()
        else:
            cli.cmd_share_revoke("bob")


def test_share_parser_routes_values_to_commands(
    tmp_path: Path, monkeypatch: Any
) -> None:
    from groken import cli, gateway, share_client, share_store

    monkeypatch.setattr(share_store, "_DEFAULT_PATH", tmp_path / "shares.json")
    monkeypatch.setattr(share_client, "_DEFAULT_PATH", tmp_path / "share.json")
    monkeypatch.setattr(gateway, "GatewayManager", lambda: FakeManager())
    monkeypatch.setattr(
        sys,
        "argv",
        ["groken", "share", "create", "--name", "bob", "--bot", "friend-bot"],
    )
    cli._main_impl()
    assert share_store.ShareStore().list() == [
        share_store.ShareRecord("bob", "agent-shared", "friend-bot")
    ]

    served: list[tuple[str, int]] = []
    monkeypatch.setattr(
        cli, "cmd_share_serve", lambda host, port: served.append((host, port))
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["groken", "share", "serve", "--host", "0.0.0.0", "--port", "9999"],
    )
    cli._main_impl()
    assert served == [("0.0.0.0", 9999)]


def test_share_cli_reports_missing_revoke_without_traceback(
    tmp_path: Path, monkeypatch: Any
) -> None:
    from groken import cli, share_client, share_store

    monkeypatch.setattr(share_store, "_DEFAULT_PATH", tmp_path / "shares.json")
    monkeypatch.setattr(share_client, "_DEFAULT_PATH", tmp_path / "share.json")
    monkeypatch.setattr(sys, "argv", ["groken", "share", "revoke", "missing"])

    with pytest.raises(SystemExit, match="share not found: missing"):
        cli._main_impl()


def test_share_link_config_is_private_and_removable(tmp_path: Path) -> None:
    path = tmp_path / "share.json"
    link = ShareLink("https://relay.example.test", "grk_share_secret")

    save_share_link(link, path)

    assert load_share_link(path) == link
    assert os.stat(path).st_mode & 0o777 == 0o600
    assert clear_share_link(path) is True
    assert load_share_link(path) is None
