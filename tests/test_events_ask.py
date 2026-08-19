import json

import httpx

import groken.gateway as gw_mod
from groken.gateway import GatewaySession


def sse_body(frames):
    out = b""
    for f in frames:
        out += b"event: message\n"
        out += b"data: " + json.dumps(f).encode() + b"\n\n"
    return out


def make_session(events_frames, tail_entries):
    s = GatewaySession.__new__(GatewaySession)
    s.gateway_url = "https://gw.example"
    s.gateway_token = "gt"
    s.network_token = "nt"
    s.pod_id = "pod-1"

    def handler(request):
        path = request.url.path
        if path.endswith("/api/sendPrompt"):
            return httpx.Response(200, json={"accepted": True})
        if path.endswith("/api/getAgentTranscriptTail"):
            return httpx.Response(200, json={"entries": tail_entries})
        if path.endswith("/api/listAgents"):
            return httpx.Response(200, json=[])
        if path.endswith("/events"):
            return httpx.Response(200, content=sse_body(events_frames),
                                  headers={"content-type": "text/event-stream"})
        return httpx.Response(404)

    s.http = httpx.Client(transport=httpx.MockTransport(handler))
    return s


def test_ask_via_events_returns_on_compose_flip():
    frames = [
        {"channel": "transcript", "payload": {"type": "appended", "agentId": "a1",
            "entry": {"kind": "message", "id": "u1", "role": "user", "content": "q"}}},
        {"channel": "agent-upserted", "payload": {"agent": {"id": "a1", "isComposingMessage": True, "isRunning": True}}},
        {"channel": "transcript", "payload": {"type": "appended", "agentId": "a1",
            "entry": {"kind": "send-message", "id": "r1", "message": {"type": "text", "content": "pong"}}}},
        {"channel": "agent-upserted", "payload": {"agent": {"id": "a1", "isComposingMessage": False, "isRunning": True}}},
    ]
    s = make_session(frames, [])
    orig_sleep = gw_mod.time.sleep
    gw_mod.time.sleep = lambda _s: None
    try:
        reply = s.ask("a1", "q", timeout_s=30)
    finally:
        gw_mod.time.sleep = orig_sleep
    assert reply == "pong"


def test_ask_via_events_ignores_stale_replayed_entries():
    frames = [
        {"channel": "transcript", "payload": {"type": "appended", "agentId": "a1",
            "entry": {"kind": "send-message", "id": "stale1", "timestampMs": 1,
                      "message": {"type": "text", "content": "old reply from previous turn"}}}},
        {"channel": "agent-upserted", "payload": {"agent": {"id": "a1", "isComposingMessage": True, "isRunning": True}}},
        {"channel": "transcript", "payload": {"type": "appended", "agentId": "a1",
            "entry": {"kind": "send-message", "id": "r1", "timestampMs": 9_999_999_999_999,
                      "message": {"type": "text", "content": "fresh pong"}}}},
        {"channel": "agent-upserted", "payload": {"agent": {"id": "a1", "isComposingMessage": False, "isRunning": True}}},
    ]
    s = make_session(frames, [])
    orig_sleep = gw_mod.time.sleep
    gw_mod.time.sleep = lambda _s: None
    try:
        reply = s.ask("a1", "q", timeout_s=30)
    finally:
        gw_mod.time.sleep = orig_sleep
    assert reply == "fresh pong"
    assert "old reply" not in reply


def test_ask_via_events_dedupes_repeated_entry_ids():
    frames = [
        {"channel": "agent-upserted", "payload": {"agent": {"id": "a1", "isComposingMessage": True}}},
        {"channel": "transcript", "payload": {"type": "appended", "agentId": "a1",
            "entry": {"kind": "send-message", "id": "r1", "timestampMs": 9_999_999_999_999,
                      "message": {"type": "text", "content": "pong"}}}},
        {"channel": "transcript", "payload": {"type": "appended", "agentId": "a1",
            "entry": {"kind": "send-message", "id": "r1", "timestampMs": 9_999_999_999_999,
                      "message": {"type": "text", "content": "pong"}}}},
        {"channel": "agent-upserted", "payload": {"agent": {"id": "a1", "isComposingMessage": False}}},
    ]
    s = make_session(frames, [])
    orig_sleep = gw_mod.time.sleep
    gw_mod.time.sleep = lambda _s: None
    try:
        reply = s.ask("a1", "q", timeout_s=30)
    finally:
        gw_mod.time.sleep = orig_sleep
    assert reply == "pong"


def test_ask_via_events_breaks_on_not_composing_without_prior_compose():
    frames = [
        {"channel": "transcript", "payload": {"type": "appended", "agentId": "a1",
            "entry": {"kind": "send-message", "id": "r1", "timestampMs": 9_999_999_999_999,
                      "message": {"type": "text", "content": "pong"}}}},
        {"channel": "agent-upserted", "payload": {"agent": {"id": "a1", "isComposingMessage": False}}},
    ]
    s = make_session(frames, [])
    orig_sleep = gw_mod.time.sleep
    gw_mod.time.sleep = lambda _s: None
    try:
        reply = s.ask("a1", "q", timeout_s=30, idle_s=999)
    finally:
        gw_mod.time.sleep = orig_sleep
    assert reply == "pong"


def test_ask_via_events_ignores_other_agents_and_falls_back_on_sse_failure():
    s = make_session([], [])
    calls = {"tail": 0}
    orig_tail = s.transcript_tail

    def tail(agent_id):
        calls["tail"] += 1
        if calls["tail"] == 1:
            return [{"kind": "send-message", "id": "old", "message": {"content": "x"}}]
        return [
            {"kind": "send-message", "id": "old", "message": {"content": "x"}},
            {"kind": "send-message", "id": "r1", "message": {"content": "via-poll"}},
        ]

    s.transcript_tail = tail
    orig_sleep = gw_mod.time.sleep
    gw_mod.time.sleep = lambda _s: None
    orig_busy = s.agent_busy
    s.agent_busy = lambda agent_id: None
    try:
        reply = s.ask("a1", "q", timeout_s=30, idle_s=0)
    finally:
        gw_mod.time.sleep = orig_sleep
        s.transcript_tail = orig_tail
        s.agent_busy = orig_busy
    assert reply == "via-poll"
