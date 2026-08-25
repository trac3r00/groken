from __future__ import annotations

from typing import Any

from groken.status import collect_status, render_status


class _Manager:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object | None]] = []

    def own_agent_id(self) -> str:
        return "groken-id"

    def command(self, method: str, args: object | None = None) -> dict[str, Any]:
        self.calls.append((method, args))
        return {
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
            "getBoxStoreStatus": {
                "durable": True,
                "entryCount": 100,
                "totalBytes": 3_831_910_425,
                "lastSnapshotAtMs": 1_700_000_000_000,
            },
            "getBoxSecretsStatus": {
                "keys": ["SHOULD_NOT_BE_RETURNED"],
                "isApplied": True,
                "lastAppliedAtMs": 1_700_000_000_000,
            },
            "listBoxMcpServers": {
                "servers": [
                    {"serverIdentifier": "ok", "status": "connected", "toolCount": 3},
                    {"serverIdentifier": "bad", "status": "error", "statusDetail": "broken", "toolCount": 0},
                ]
            },
        }[method]


def test_collect_status_uses_configured_bot_and_safe_fields() -> None:
    manager = _Manager()

    status = collect_status(manager)

    assert manager.calls == [
        ("getForeverBoxStatus", {"id": "groken-id"}),
        ("getHostStatus", None),
        ("getBoxStoreStatus", None),
        ("getBoxSecretsStatus", None),
        ("listBoxMcpServers", {"serverIdentifiers": []}),
    ]
    assert status["bot"] == {"id": "groken-id", "state": "running", "display": 2}
    storage = status["storage"]
    assert isinstance(storage, dict)
    assert storage["total_bytes"] == 3_831_910_425
    assert status["secrets"] == {"is_applied": True, "last_applied_at_ms": 1_700_000_000_000}
    assert status["mcp"] == {"connected": 1, "errors": 1, "servers": [
        {"id": "ok", "status": "connected", "tool_count": 3, "detail": None},
        {"id": "bad", "status": "error", "tool_count": 0, "detail": "broken"},
    ]}


def test_render_status_is_concise_and_surfaces_failures() -> None:
    text = render_status(collect_status(_Manager()))

    assert "Bot: groken-id (running, display :2)" in text
    assert "Host: abc123 (current, busy)" in text
    assert "Storage: 3.57 GiB" in text
    assert "MCP: 1 connected, 1 error" in text
    assert "bad: error - broken" in text
    assert "SHOULD_NOT_BE_RETURNED" not in text
