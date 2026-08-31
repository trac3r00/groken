import base64
import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import Event, Thread
from types import TracebackType
from typing import Self

import pytest
from fastapi.testclient import TestClient
from typing_extensions import override

import groken.share_server as share_server_module
from groken.exec_service import ExecResult
from groken.share_server import ShareManager, create_share_app
from groken.share_store import ShareRecord, ShareStore


class FakeFeed:
    def __init__(self, events: Iterator[dict[str, object]]) -> None:
        self.events = events
        self.timeouts: list[float | None] = []

    def next_event(
        self, timeout_s: float | None, *, hold: bool = False
    ) -> dict[str, object]:
        del hold
        self.timeouts.append(timeout_s)
        return next(self.events)

    def resume(self) -> None:
        return None


class FakeManager:
    def __init__(self) -> None:
        self.commands: list[tuple[str, dict[str, object]]] = []
        self.event_channels: list[str] | None = None
        self.subscription_timeout: float | None = 999.0
        self.command_result: object = {"ok": True}
        self.feed: FakeFeed | None = None
        self.box_status: dict[str, object] = {
            "vncUrl": (
                "https://viewer.example.test/vnc.html?path=websockify%3Ftoken%3D2"
            )
        }

    def close(self) -> None:
        return None

    def resolve_agent(self, bot: str | None = None) -> str:
        raise AssertionError(f"mutable name resolution must not run: {bot}")

    def command(self, method: str, args: dict[str, object] | None = None) -> object:
        captured = args or {}
        self.commands.append((method, captured))
        if method in {"getForeverBoxStatus", "ensureForeverBox"}:
            return self.box_status
        return self.command_result

    def send_prompt(self, agent_id: str, text: str) -> dict[str, object]:
        return {
            "accepted": True,
            "agentId": agent_id,
            "text": text,
            "otherAgents": [{"id": "private"}],
        }

    def ask(self, agent_id: str, text: str, timeout_s: float = 600) -> str:
        return f"{agent_id}:{text}:{timeout_s}"

    def ask_stream(
        self,
        agent_id: str,
        text: str,
        timeout_s: float = 600,
        idle_s: float = 45,
        on_chunk: Callable[[str], None] | None = None,
    ) -> str:
        del agent_id, text, timeout_s, idle_s
        if on_chunk is not None:
            on_chunk("first")
            on_chunk("forbidden")
        return "complete"

    def transcript_tail(self, agent_id: str) -> list[dict[str, object]]:
        return [
            {
                "id": "m1",
                "kind": "assistant",
                "timestampMs": 1,
                "content": "hello",
                "agentId": agent_id,
                "otherAgents": [{"id": "private"}],
            }
        ]

    def ensure_sandbox_metadata(self) -> dict[str, object]:
        return {
            "vncUrl": "https://tenant-pod-6080.example.test/vnc.html",
            "forkVncBaseUrl": "https://tenant-pod-6081.example.test",
            "networkToken": "network-secret",
            "execDaemonAuthToken": "exec-secret",
            "gatewayToken": "gateway-secret",
            "accessToken": "oauth-secret",
            "podId": "pod-1",
        }

    def events(self, channels: list[str] | None = None) -> Iterator[dict[str, object]]:
        self.event_channels = channels
        yield {
            "event": "agent-update",
            "data": {
                "payload": {"agentId": "bot-id", "text": "first"},
                "networkToken": "network-secret",
                "otherAgents": [{"id": "private", "name": "Private"}],
            },
        }
        yield {
            "event": "agent-update",
            "data": {"payload": {"agentId": "bot-id", "text": "forbidden"}},
        }

    @contextmanager
    def event_subscription(
        self, channels: list[str], timeout_s: float | None
    ) -> Iterator[FakeFeed]:
        self.subscription_timeout = timeout_s
        self.event_channels = channels
        self.feed = FakeFeed(self.events(channels))
        yield self.feed


class TimeoutFeed(FakeFeed):
    def __init__(self) -> None:
        super().__init__(iter(()))

    def next_event(
        self, timeout_s: float | None, *, hold: bool = False
    ) -> dict[str, object]:
        del hold
        self.timeouts.append(timeout_s)
        raise TimeoutError


class TimeoutManager(FakeManager):
    @contextmanager
    def event_subscription(
        self, channels: list[str], timeout_s: float | None
    ) -> Iterator[FakeFeed]:
        self.subscription_timeout = timeout_s
        self.event_channels = channels
        self.feed = TimeoutFeed()
        yield self.feed


class IdleAskManager(FakeManager):
    def __init__(self) -> None:
        super().__init__()
        self.started: Event = Event()
        self.closed: Event = Event()

    @override
    def ask_stream(
        self,
        agent_id: str,
        text: str,
        timeout_s: float = 600,
        idle_s: float = 45,
        on_chunk: Callable[[str], None] | None = None,
    ) -> str:
        del agent_id, text, timeout_s, idle_s, on_chunk
        self.started.set()
        if not self.closed.wait(2.0):
            raise AssertionError("stream manager was not closed")
        return "late reply"

    @override
    def close(self) -> None:
        self.closed.set()


class NestedEventManager(FakeManager):
    def events(self, channels: list[str] | None = None) -> Iterator[dict[str, object]]:
        self.event_channels = channels
        yield {
            "event": "agent-update",
            "data": {
                "agentId": "bot-id",
                "content": {"otherAgents": [{"id": "private"}]},
            },
        }


class CredentialEventManager(FakeManager):
    def events(self, channels: list[str] | None = None) -> Iterator[dict[str, object]]:
        self.event_channels = channels
        yield {
            "event": "agent-update",
            "data": {
                "agentId": "bot-id",
                "text": "direct",
                "vncUrl": "https://vnc.test/vnc.html?port_token=direct-secret",
                "port_token": "direct-secret",
            },
        }
        yield {
            "event": "agent-update",
            "data": {
                "payload": {
                    "agentId": "bot-id",
                    "text": "nested",
                    "vncUrl": "https://vnc.test/vnc.html?port_token=nested-secret",
                    "port_token": "nested-secret",
                }
            },
        }


class FakeExecRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int]] = []

    async def execute(
        self,
        command: str,
        working_directory: str = "/workspace",
        timeout_ms: int = 15000,
    ) -> ExecResult:
        self.calls.append((command, working_directory, timeout_ms))
        return ExecResult("stdout", "stderr", 7)


class RevocableStore:
    def __init__(self, record: ShareRecord) -> None:
        self.record: ShareRecord = record
        self.active: Event = Event()
        self.active.set()

    def authenticate(self, token: str) -> ShareRecord | None:
        assert token == "share-token"
        return self.record if self.active.is_set() else None

    def revoke(self) -> None:
        self.active.clear()


class OwnedExecClient(FakeExecRunner):
    def __init__(self, *, fails: bool) -> None:
        super().__init__()
        self.fails: bool = fails
        self.closed: bool = False

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback
        self.closed = True

    @override
    async def execute(
        self,
        command: str,
        working_directory: str = "/workspace",
        timeout_ms: int = 15000,
    ) -> ExecResult:
        if self.fails:
            raise RuntimeError("exec failed")
        return await super().execute(command, working_directory, timeout_ms)


class SequencedStore:
    def __init__(self, record: ShareRecord, active: list[bool]) -> None:
        self.record = record
        self.active = iter(active)

    def authenticate(self, token: str) -> ShareRecord | None:
        assert token == "share-token"
        return self.record if next(self.active, False) else None


def auth(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


def make_client(
    tmp_path: Path,
    manager: FakeManager,
    *,
    exec_runner: FakeExecRunner | None = None,
) -> tuple[TestClient, ShareStore, str]:
    store = ShareStore(tmp_path / "shares.json")
    grant = store.create("alice", "bot-id", "Mutable Name")
    factory = None if exec_runner is None else lambda _: exec_runner
    app = create_share_app(lambda: manager, store, exec_factory=factory)
    return TestClient(app), store, grant.token


def test_bot_identity_is_pinned_without_name_resolution(tmp_path: Path) -> None:
    manager = FakeManager()
    client, _, token = make_client(tmp_path, manager)

    response = client.get("/v1/bot", headers=auth(token))

    assert response.json() == {"agent_id": "bot-id", "name": "Mutable Name"}


def test_metadata_route_is_absent(tmp_path: Path) -> None:
    client, _, token = make_client(tmp_path, FakeManager())

    response = client.get("/v1/metadata", headers=auth(token))

    assert response.status_code == 404


def test_lazy_manager_closes_after_nonstream_response() -> None:
    # Given
    manager = IdleAskManager()
    record = ShareRecord("alice", "bot-id", "Mutable Name", False)
    store = SequencedStore(record, [True, True])
    app = create_share_app(lambda: manager, store)

    # When
    response = TestClient(app).post(
        "/v1/send", headers=auth("share-token"), json={"text": "hello"}
    )

    # Then
    assert response.status_code == 200
    assert manager.closed.is_set()


@pytest.mark.parametrize(("fails", "expected_status"), [(False, 200), (True, 500)])
def test_per_request_exec_client_closes_after_success_and_error(
    monkeypatch: pytest.MonkeyPatch, fails: bool, expected_status: int
) -> None:
    # Given
    runner = OwnedExecClient(fails=fails)
    record = ShareRecord("alice", "bot-id", "Mutable Name", False)
    store = SequencedStore(record, [True, True])

    def exec_client_factory(_manager: ShareManager) -> OwnedExecClient:
        return runner

    monkeypatch.setattr(
        share_server_module, "ExecServiceClient", exec_client_factory
    )
    app = create_share_app(lambda: FakeManager(), store)

    # When
    response = TestClient(app, raise_server_exceptions=False).post(
        "/v1/exec", headers=auth("share-token"), json={"command": "pwd"}
    )

    # Then
    assert response.status_code == expected_status
    assert runner.closed


def test_exec_runs_server_side_and_returns_only_result(tmp_path: Path) -> None:
    runner = FakeExecRunner()
    client, _, token = make_client(tmp_path, FakeManager(), exec_runner=runner)

    response = client.post(
        "/v1/exec",
        headers=auth(token),
        json={"command": "pwd", "cwd": "/work", "timeout_ms": 3210},
    )

    assert response.json() == {"stdout": "stdout", "stderr": "stderr", "exit_code": 7}
    assert runner.calls == [("pwd", "/work", 3210)]
    assert "secret" not in response.text


@pytest.mark.parametrize(
    ("method", "path", "payload", "forbidden"),
    [
        ("POST", "/v1/send", {"text": "hello"}, "bot-id"),
        ("POST", "/v1/ask", {"text": "hello"}, "bot-id:hello"),
        ("GET", "/v1/transcript", None, "bot-id"),
    ],
)
def test_nonstream_output_is_withheld_after_revocation(
    method: str,
    path: str,
    payload: dict[str, object] | None,
    forbidden: str,
) -> None:
    record = ShareRecord("alice", "bot-id", "Mutable Name", False)
    store = SequencedStore(record, [True, False])
    app = create_share_app(lambda: FakeManager(), store)

    response = TestClient(app).request(
        method, path, headers=auth("share-token"), json=payload
    )

    assert response.status_code == 401
    assert forbidden not in response.text


def test_exec_output_is_withheld_after_revocation() -> None:
    manager = FakeManager()
    runner = FakeExecRunner()
    record = ShareRecord("alice", "bot-id", "Mutable Name", False)
    store = SequencedStore(record, [True, False])
    app = create_share_app(lambda: manager, store, exec_factory=lambda _: runner)

    response = TestClient(app).post(
        "/v1/exec", headers=auth("share-token"), json={"command": "pwd"}
    )

    assert response.status_code == 401
    assert "stdout" not in response.text


def test_vnc_mints_only_the_pinned_bots_sixty_second_url(tmp_path: Path) -> None:
    manager = FakeManager()
    client, _, token = make_client(tmp_path, manager)

    response = client.post("/v1/vnc", headers=auth(token), json={})

    payload = response.json()
    assert set(payload) == {"url"}
    assert "secret" not in response.text
    encoded_claims = payload["url"].split("port_token=", 1)[1].split(".", 2)[1]
    claims = json.loads(base64.urlsafe_b64decode(encoded_claims + "=="))
    assert claims["exp"] - claims["iat"] == 60
    assert claims["container_port"] == 6081
    assert manager.commands == [("getForeverBoxStatus", {"id": "bot-id"})]

    rejected = client.post("/v1/vnc", headers=auth(token), json={"display": 999})
    assert rejected.status_code == 422


def test_transcript_drops_non_text_control_entries(tmp_path: Path) -> None:
    manager = FakeManager()

    def transcript_tail(agent_id: str) -> list[dict[str, object]]:
        assert agent_id == "bot-id"
        return [
            {
                "id": "control",
                "kind": "send-message",
                "timestampMs": 1,
                "message": {
                    "type": "auto-review-approval",
                    "approval": {"surface": "computer"},
                },
            },
            {
                "id": "text",
                "kind": "send-message",
                "timestampMs": 2,
                "message": {"type": "text", "content": "visible"},
            },
        ]

    manager.transcript_tail = transcript_tail
    client, _, token = make_client(tmp_path, manager)

    response = client.get("/v1/transcript", headers=auth(token))

    assert response.status_code == 200
    assert response.json() == [
        {
            "id": "text",
            "kind": "send-message",
            "timestampMs": 2,
            "message": {"content": "visible"},
        }
    ]


def test_send_transcript_and_events_drop_unknown_sibling_data(tmp_path: Path) -> None:
    client, _, token = make_client(tmp_path, FakeManager())

    sent = client.post("/v1/send", headers=auth(token), json={"text": "hello"})
    transcript = client.get("/v1/transcript", headers=auth(token))
    events = client.get("/v1/events", headers=auth(token))

    assert sent.json() == {"accepted": True}
    assert transcript.json() == [
        {
            "id": "m1",
            "kind": "assistant",
            "timestampMs": 1,
            "content": "hello",
        }
    ]
    assert "otherAgents" not in events.text
    assert "Private" not in events.text


def test_nested_values_inside_allowed_event_fields_are_rejected(
    tmp_path: Path,
) -> None:
    client, _, token = make_client(tmp_path, NestedEventManager())

    response = client.get("/v1/events", headers=auth(token))

    assert response.status_code == 200
    assert response.text == ""


def test_event_projection_drops_vnc_credentials_at_every_level_when_streamed(
    tmp_path: Path,
) -> None:
    # Given
    client, _, token = make_client(tmp_path, CredentialEventManager())

    # When
    response = client.get("/v1/events", headers=auth(token))

    # Then
    frames = [
        json.loads(lines[1].removeprefix("data: "))
        for block in response.text.strip().split("\n\n")
        if len(lines := block.splitlines()) == 2
    ]
    assert frames == [
        {"agentId": "bot-id", "text": "direct"},
        {"payload": {"agentId": "bot-id", "text": "nested"}},
    ]
    assert "vncUrl" not in response.text
    assert "port_token" not in response.text
    assert "direct-secret" not in response.text
    assert "nested-secret" not in response.text


def test_list_agents_forces_id_and_projects_safe_fields(tmp_path: Path) -> None:
    manager = FakeManager()
    manager.command_result = [
        {"id": "bot-id", "name": "Shared", "gatewayToken": "secret"},
        {"id": "other-id", "name": "Private"},
    ]
    client, _, token = make_client(tmp_path, manager)

    response = client.post(
        "/v1/command",
        headers=auth(token),
        json={"method": "listAgents", "args": {"id": "other-id"}},
    )

    assert response.json() == [{"id": "bot-id", "name": "Shared"}]
    assert manager.commands == [("listAgents", {"id": "bot-id"})]
    assert "secret" not in response.text


def test_list_agents_fails_closed_on_malformed_upstream(tmp_path: Path) -> None:
    manager = FakeManager()
    manager.command_result = [{"id": "bot-id", "gatewayToken": "secret"}]
    client, _, token = make_client(tmp_path, manager)

    response = client.post(
        "/v1/command",
        headers=auth(token),
        json={"method": "listAgents", "args": {}},
    )

    assert response.status_code == 502
    assert "secret" not in response.text


def test_ask_stream_uses_named_frames_and_stops_on_revocation() -> None:
    record = ShareRecord("alice", "bot-id", "Mutable Name", False)
    store = SequencedStore(record, [True, True, False])
    app = create_share_app(lambda: FakeManager(), store)

    response = TestClient(app).post(
        "/v1/ask/stream",
        headers=auth("share-token"),
        json={"text": "hello"},
    )

    assert 'event: chunk\ndata: {"text": "first"}' in response.text
    assert "forbidden" not in response.text
    assert "event: done" not in response.text
    assert "event: error" in response.text


def test_idle_ask_stream_rechecks_revocation_and_closes_manager() -> None:
    # Given
    manager = IdleAskManager()
    record = ShareRecord("alice", "bot-id", "Mutable Name", False)
    store = RevocableStore(record)
    app = create_share_app(lambda: manager, store, event_heartbeat_s=0.01)
    completed = Event()
    responses: list[str] = []

    def request_stream() -> None:
        response = TestClient(app).post(
            "/v1/ask/stream",
            headers=auth("share-token"),
            json={"text": "hello"},
        )
        responses.append(response.text)
        completed.set()

    request_thread = Thread(target=request_stream)
    request_thread.start()
    try:
        assert manager.started.wait(1.0)

        # When
        store.revoke()

        # Then
        assert completed.wait(1.0)
        assert responses == ['event: error\ndata: {"detail": "share revoked"}\n\n']
        assert manager.closed.is_set()
    finally:
        manager.closed.set()
        request_thread.join(1.0)


def test_idle_event_stream_rechecks_revocation_on_heartbeat() -> None:
    manager = TimeoutManager()
    record = ShareRecord("alice", "bot-id", "Mutable Name", False)
    store = SequencedStore(record, [True, False])
    app = create_share_app(lambda: manager, store, event_heartbeat_s=7.0)

    response = TestClient(app).get(
        "/v1/events?channels=agent", headers=auth("share-token")
    )

    assert response.status_code == 200
    assert response.text == ""
    assert manager.feed is not None
    assert manager.feed.timeouts == [7.0]
    assert manager.subscription_timeout is None


def test_events_pass_channels_preserve_frames_and_stop_on_revocation() -> None:
    manager = FakeManager()
    record = ShareRecord("alice", "bot-id", "Mutable Name", False)
    store = SequencedStore(record, [True, True, False])
    app = create_share_app(lambda: manager, store)

    response = TestClient(app).get(
        "/v1/events?channels=agent,forever-box", headers=auth("share-token")
    )

    frames = [
        (lines[0].removeprefix("event: "), json.loads(lines[1].removeprefix("data: ")))
        for block in response.text.strip().split("\n\n")
        if len(lines := block.splitlines()) == 2
    ]

    assert manager.event_channels == ["agent", "forever-box"]
    assert frames == [
        ("agent-update", {"payload": {"agentId": "bot-id", "text": "first"}})
    ]
    assert "network-secret" not in response.text
    assert "forbidden" not in response.text
