from __future__ import annotations

from pathlib import Path

import pytest

from groken.status import collect_status, render_status

_MALICIOUS_ROUTINE_NAME = (
    "bad-TOKEN_task9_secret-\\private\\path-\x1b[31m\nnewline-marker"
)


class _Manager:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object | None]] = []

    def own_agent_id(self) -> str:
        return "groken-id"

    def command(
        self, method: str, args: dict[str, object] | None = None
    ) -> dict[str, object]:
        self.calls.append((method, args))
        responses: dict[str, dict[str, object]] = {
            "getForeverBoxStatus": {
                "agentId": "groken-id",
                "state": "running",
                "vncUrl": "http://127.0.0.1/vnc.html?path=websockify%3Ftoken%3D2",
            },
            "getHostStatus": {
                "hostVersion": "abc123",
                "latestHostVersion": "abc123",
                "hostUpdateAvailable": False,
                "isBusy": True,
                "capabilities": ["orderedReplicasV1"],
            },
            "getBoxSecretsStatus": {
                "keys": ["SHOULD_NOT_BE_RETURNED"],
                "isApplied": True,
                "lastAppliedAtMs": 1_700_000_000_000,
            },
            "listBoxMcpServers": {
                "servers": [
                    {"serverIdentifier": "ok", "status": "connected", "toolCount": 3},
                    {
                        "serverIdentifier": "bad",
                        "status": "error",
                        "statusDetail": "broken",
                        "toolCount": 0,
                    },
                ]
            },
        }
        return responses[method]


def test_collect_status_uses_configured_bot_and_safe_fields() -> None:
    manager = _Manager()

    status = collect_status(manager)

    assert manager.calls == [
        ("getForeverBoxStatus", {"id": "groken-id"}),
        ("getHostStatus", None),
        ("getBoxSecretsStatus", None),
        ("listBoxMcpServers", {"serverIdentifiers": []}),
    ]
    assert status["bot"] == {"id": "groken-id", "state": "running", "display": 2}
    assert "storage" not in status
    assert status["secrets"] == {
        "is_applied": True,
        "last_applied_at_ms": 1_700_000_000_000,
    }
    assert status["mcp"] == {
        "connected": 1,
        "errors": 1,
        "servers": [
            {"id": "ok", "status": "connected", "tool_count": 3, "detail": None},
            {"id": "bad", "status": "error", "tool_count": 0, "detail": "broken"},
        ],
    }


def test_render_status_is_concise_and_surfaces_failures() -> None:
    text = render_status(collect_status(_Manager()))

    assert "Bot: groken-id (running, display :2)" in text
    assert "Host: abc123 (current, busy)" in text
    assert "Storage:" not in text
    assert "MCP: 1 connected, 1 error" in text
    assert "bad: error - broken" in text
    assert "SHOULD_NOT_BE_RETURNED" not in text


def test_status_continues_after_corrupt_local_routine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    routine = tmp_path / ".config/groken/routines/bad"
    routine.mkdir(parents=True)
    _ = (routine / "routine.toml").write_text('name = "unterminated')
    _ = (routine / "run.sh").write_text("#!/bin/sh\nexit 0\n")

    text = render_status(collect_status(_Manager()))

    assert "Bot: groken-id (running, display :2)" in text
    assert "Routines: 0 healthy, 1 corrupt" in text
    assert "Environment: missing" in text
    assert "Lifecycle/swarm: available" in text


def test_status_never_renders_malicious_corrupt_routine_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    routine = tmp_path / ".config/groken/routines" / _MALICIOUS_ROUTINE_NAME
    routine.mkdir(parents=True)
    _ = (routine / "routine.toml").write_text('name = "unterminated')
    _ = (routine / "run.sh").write_text("#!/bin/sh\nexit 0\n")

    text = render_status(collect_status(_Manager()))

    assert "Routines: 0 healthy, 1 corrupt" in text
    assert _MALICIOUS_ROUTINE_NAME not in text
    assert "TOKEN_task9_secret" not in text
    assert "private\\path" not in text
    assert "newline-marker" not in text
    assert "\x1b" not in text
