import json
from collections.abc import Callable, Iterator, Mapping, Sequence
from typing import cast, final

import httpx
import pytest
from typing_extensions import override

from groken.client import ConnectError
from groken.gateway import GatewayManager, GatewaySession

JsonObject = Mapping[str, object]


@final
class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


@final
class AnchoredTimeoutEvents(httpx.SyncByteStream):
    def __init__(self, clock: FakeClock, frame: JsonObject) -> None:
        self.clock = clock
        self.frame = frame

    @override
    def __iter__(self) -> Iterator[bytes]:
        yield b"event: message\ndata: " + json.dumps(self.frame).encode() + b"\n\n"
        self.clock.sleep(3)
        raise httpx.ReadTimeout("idle event stream")


class ScriptedEvents(httpx.SyncByteStream):
    def __init__(
        self,
        clock: FakeClock,
        frames: Sequence[tuple[float, JsonObject | None]],
    ) -> None:
        self.clock: FakeClock = clock
        self.frames: Sequence[tuple[float, JsonObject | None]] = frames

    @override
    def __iter__(self) -> Iterator[bytes]:
        for delay, frame in self.frames:
            self.clock.sleep(delay)
            if frame is None:
                yield b": tick\n\n"
            else:
                yield b"event: message\ndata: " + json.dumps(frame).encode() + b"\n\n"


def appended(entry_id: str, content: str) -> dict[str, object]:
    return {
        "channel": "transcript",
        "payload": {
            "type": "appended",
            "agentId": "a1",
            "entry": {
                "kind": "send-message",
                "id": entry_id,
                "message": {"type": "text", "content": content},
            },
        },
    }


def transcript_entry(entry_id: str, content: str) -> dict[str, object]:
    return {
        "kind": "send-message",
        "id": entry_id,
        "timestampMs": 9999999999999,
        "message": {"type": "text", "content": content},
    }


def upsert(composing: bool, running: bool) -> dict[str, object]:
    return {
        "channel": "agent-upserted",
        "payload": {
            "agent": {
                "id": "a1",
                "isComposingMessage": composing,
                "isRunning": running,
            },
        },
    }


def make_session(
    frames: Sequence[tuple[float, JsonObject | None]],
    *,
    tails: Sequence[Sequence[JsonObject]] | None = None,
    agents: Sequence[Sequence[JsonObject]] | None = None,
    events_status: int = 200,
) -> tuple[GatewaySession, FakeClock, list[dict[str, object]]]:
    clock = FakeClock()
    prompts: list[dict[str, object]] = []
    tail_script: list[Sequence[JsonObject]] = list(tails or [[]])
    agent_script: list[Sequence[JsonObject]] = list(agents or [[]])

    def next_value(script: list[Sequence[JsonObject]]) -> Sequence[JsonObject]:
        if len(script) > 1:
            return script.pop(0)
        return script[0]

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/api/sendPrompt"):
            prompts.append(cast(dict[str, object], json.loads(request.content)))
            return httpx.Response(200, json={"accepted": True})
        if path.endswith("/api/getAgentTranscriptTail"):
            return httpx.Response(200, json={"entries": next_value(tail_script)})
        if path.endswith("/api/listAgents"):
            return httpx.Response(200, json=next_value(agent_script))
        if path.endswith("/events"):
            if events_status != 200:
                return httpx.Response(events_status, text="events unavailable")
            return httpx.Response(
                200,
                stream=ScriptedEvents(clock, frames),
                headers={"content-type": "text/event-stream"},
            )
        return httpx.Response(404)

    session = GatewaySession(
        "https://gw.example",
        "gt",
        "nt",
        "pod-1",
        monotonic=clock.monotonic,
        sleeper=clock.sleep,
    )
    session.http = httpx.Client(transport=httpx.MockTransport(handler))
    return session, clock, prompts


def test_events_subscribe_before_sending_prompt() -> None:
    clock = FakeClock()
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("/events"):
            return httpx.Response(
                200,
                stream=ScriptedEvents(
                    clock,
                    [
                        (0, appended("r1", "answer")),
                        (0, upsert(False, False)),
                        (2, upsert(False, False)),
                    ],
                ),
            )
        if request.url.path.endswith("/api/sendPrompt"):
            return httpx.Response(200, json={"accepted": True})
        return httpx.Response(404)

    session = GatewaySession(
        "https://gw.example",
        "gt",
        "nt",
        "pod-1",
        monotonic=clock.monotonic,
        sleeper=clock.sleep,
    )
    session.http = httpx.Client(transport=httpx.MockTransport(handler))

    assert session.ask("a1", "question", timeout_s=30) == "answer"
    assert paths.index("/events") < paths.index("/api/sendPrompt")


def test_anchored_reply_recovers_when_terminal_upserts_never_arrive() -> None:
    clock = FakeClock()
    prompt_count = 0
    reply = transcript_entry("r1", "answer")

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal prompt_count
        if request.url.path.endswith("/events"):
            return httpx.Response(
                200,
                stream=AnchoredTimeoutEvents(clock, appended("r1", "answer")),
            )
        if request.url.path.endswith("/api/sendPrompt"):
            prompt_count += 1
            return httpx.Response(200, json={"accepted": True})
        if request.url.path.endswith("/api/getAgentTranscriptTail"):
            return httpx.Response(200, json={"entries": [reply]})
        if request.url.path.endswith("/api/listAgents"):
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "a1",
                        "isComposingMessage": False,
                        "isRunning": False,
                    }
                ],
            )
        return httpx.Response(404)

    session = GatewaySession(
        "https://gw.example",
        "gt",
        "nt",
        "pod-1",
        monotonic=clock.monotonic,
        sleeper=clock.sleep,
    )
    session.http = httpx.Client(transport=httpx.MockTransport(handler))

    assert session.ask("a1", "question", timeout_s=30) == "answer"
    assert prompt_count == 1


def test_transcript_tail_sends_required_limit() -> None:
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(cast(dict[str, object], json.loads(request.content)))
        return httpx.Response(200, json={"entries": []})

    session = GatewaySession("https://gw.example", "gt", "nt", "pod-1")
    session.http = httpx.Client(transport=httpx.MockTransport(handler))

    assert session.transcript_tail("a1") == []
    assert requests == [{"id": "a1", "limit": 100}]


def test_events_collect_late_append_after_busy_false_and_two_fast_stable_ticks() -> (
    None
):
    session, _, prompts = make_session(
        [
            (0, upsert(True, True)),
            (0, appended("r1", "partial")),
            (0, upsert(False, False)),
            (1, upsert(False, False)),
            # Two stable idle upserts are not enough until the reply has been quiet for 2s.
            (0, appended("r2", "final")),
            (0, upsert(False, False)),
            (2, upsert(False, False)),
        ]
    )

    assert session.ask("a1", "q", timeout_s=30, idle_s=20) == "partial\nfinal"
    assert len(prompts) == 1


def test_ask_stream_emits_chunks_in_order_and_returns_full_reply() -> None:
    session, _, prompts = make_session(
        [
            (0, upsert(True, True)),
            (0, upsert(False, False)),
            (0, appended("r1", "first")),
            (0, appended("r2", "second")),
            (0, upsert(False, False)),
            (2, upsert(False, False)),
        ]
    )
    chunks: list[str] = []

    assert (
        session.ask_stream("a1", "q", timeout_s=30, on_chunk=chunks.append)
        == "first\nsecond"
    )
    assert chunks == ["first", "second"]
    assert len(prompts) == 1


def test_ask_stream_partial_timeout_raises() -> None:
    session, _, _ = make_session(
        [
            (0, upsert(True, True)),
            (0, appended("r1", "partial")),
            (0, upsert(False, False)),
            (4, None),
        ]
    )
    with pytest.raises(ConnectError, match="reply incomplete"):
        _ = session.ask_stream("a1", "q", timeout_s=3, on_chunk=lambda _chunk: None)


def test_manager_ask_stream_delegates_to_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[object, ...]] = []

    @final
    class Session(GatewaySession):
        def __init__(self) -> None:
            super().__init__("https://gw.example", "gt", "nt", "pod-1")

        @override
        def ask_stream(
            self,
            agent_id: str,
            text: str,
            timeout_s: float = 600,
            idle_s: float = 45,
            client_nonce: str | None = None,
            on_chunk: Callable[[str], None] | None = None,
        ) -> str:
            calls.append((agent_id, text, timeout_s, idle_s, client_nonce, on_chunk))
            return "reply"

    manager = GatewayManager.__new__(GatewayManager)
    session = Session()
    monkeypatch.setattr(manager, "session", lambda force=False: session)
    chunks: list[str] = []
    assert manager.ask_stream("a1", "q", on_chunk=chunks.append) == "reply"
    assert calls[0][0:2] == ("a1", "q")
    assert callable(calls[0][-1])


def test_timeout_with_partial_chunks_raises_instead_of_returning_partial() -> None:
    session, clock, prompts = make_session(
        [
            (0, upsert(True, True)),
            (0, appended("r1", "partial")),
            (0, upsert(False, False)),  # only one stable observation
            (4, None),
        ]
    )

    with pytest.raises(ConnectError, match="reply incomplete") as raised:
        _ = session.ask("a1", "q", timeout_s=3, idle_s=20)

    assert raised.value.body == "reply incomplete"
    assert clock.now == 4
    assert len(prompts) == 1


def test_idle_expiry_with_partial_chunks_raises() -> None:
    session, clock, prompts = make_session(
        [
            (0, upsert(True, True)),
            (0, appended("r1", "partial")),
            (0, upsert(False, False)),
            (3, None),
        ]
    )

    with pytest.raises(ConnectError, match="reply incomplete"):
        _ = session.ask("a1", "q", timeout_s=30, idle_s=2)

    assert clock.now == 3
    assert len(prompts) == 1


def test_composing_false_while_running_true_does_not_complete() -> None:
    session, _, _ = make_session(
        [
            (0, upsert(True, True)),
            (0, appended("r1", "partial")),
            (2, upsert(False, True)),
            (0, appended("r2", "final")),
            (0, upsert(False, False)),
            (2, upsert(False, False)),
        ]
    )

    assert session.ask("a1", "q", timeout_s=30, idle_s=20) == "partial\nfinal"


def require_object(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return cast(dict[str, object], value)


def test_events_dedupe_and_ignore_stale_or_other_agent_entries() -> None:
    stale = appended("stale", "old")
    stale_payload = require_object(stale["payload"])
    stale_entry = require_object(stale_payload["entry"])
    stale_entry["timestampMs"] = 1
    other = appended("other", "wrong agent")
    other_payload = require_object(other["payload"])
    other_payload["agentId"] = "a2"
    reply = appended("r1", "fresh")
    session, _, _ = make_session(
        [
            (0, stale),
            (0, other),
            (0, upsert(True, True)),
            (0, reply),
            (0, reply),
            (0, upsert(False, False)),
            (2, upsert(False, False)),
        ]
    )

    assert session.ask("a1", "q", timeout_s=30, idle_s=20) == "fresh"


def test_poll_fallback_requires_two_stable_tails_and_authoritative_idle() -> None:
    old = {"kind": "send-message", "id": "old", "message": {"content": "old"}}
    fresh = {"kind": "send-message", "id": "r1", "message": {"content": "via-poll"}}
    idle = [{"id": "a1", "isComposingMessage": False, "isRunning": False}]
    session, clock, prompts = make_session(
        [],
        events_status=404,
        tails=[[old], [old, fresh], [old, fresh]],
        agents=[idle, idle],
    )

    assert session.ask("a1", "q", timeout_s=30, idle_s=20) == "via-poll"
    assert clock.now >= 4
    assert len(prompts) == 1


def test_poll_treats_running_true_as_busy_after_composing_stops() -> None:
    partial = {"kind": "send-message", "id": "r1", "message": {"content": "partial"}}
    final = {"kind": "send-message", "id": "r2", "message": {"content": "final"}}
    running = [{"id": "a1", "isComposingMessage": False, "isRunning": True}]
    idle = [{"id": "a1", "isComposingMessage": False, "isRunning": False}]
    session, _, prompts = make_session(
        [],
        events_status=404,
        tails=[
            [],
            [partial],
            [partial],
            [partial, final],
            [partial, final],
            [partial, final],
        ],
        agents=[running, running, running, idle, idle],
    )

    assert session.ask("a1", "q", timeout_s=30, idle_s=20) == "partial\nfinal"
    assert len(prompts) == 1


def test_poll_one_stable_tail_then_timeout_raises() -> None:
    fresh = {"kind": "send-message", "id": "r1", "message": {"content": "partial"}}
    idle = [{"id": "a1", "isComposingMessage": False, "isRunning": False}]
    session, _, prompts = make_session(
        [],
        events_status=404,
        tails=[[], [fresh], [fresh], [fresh]],
        agents=[idle, [], []],
    )

    with pytest.raises(ConnectError, match="reply incomplete"):
        _ = session.ask("a1", "q", timeout_s=5, idle_s=20)

    assert len(prompts) == 1


def test_post_send_event_fallback_does_not_resend_prompt() -> None:
    fresh = {"kind": "send-message", "id": "r1", "message": {"content": "via-poll"}}
    idle = [{"id": "a1", "isComposingMessage": False, "isRunning": False}]
    session, _, prompts = make_session(
        [],
        tails=[[], [fresh], [fresh]],
        agents=[idle, idle],
    )

    assert (
        session.ask(
            "a1",
            "q",
            timeout_s=30,
            idle_s=20,
            client_nonce="nonce-1",
        )
        == "via-poll"
    )
    assert len(prompts) == 1
    assert prompts[0]["prompt"] == "q"
    assert prompts[0]["clientNonce"] == "nonce-1"
