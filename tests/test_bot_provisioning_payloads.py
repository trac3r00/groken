from collections.abc import Callable
from typing import Protocol, cast

import pytest

from groken.client import ConnectError
from groken.gateway import GatewayManager
from groken.provisioning import WORKER_DESCRIPTION


class LifecycleManager(Protocol):
    def create_bot(self, name: str) -> dict[str, object]: ...

    def _provision_bot(
        self, agent: dict[str, object], name: str | None = None
    ) -> dict[str, object]: ...


def manager() -> LifecycleManager:
    return cast(
        "LifecycleManager", cast("object", GatewayManager.__new__(GatewayManager))
    )


def test_create_bot_uses_requested_name_in_required_update_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    lifecycle = manager()
    calls: list[tuple[str, dict[str, object] | None]] = []

    def command(method: str, args: dict[str, object] | None = None) -> object:
        calls.append((method, args))
        return {"agent": {"id": "new-1"}} if method == "createAgent" else None

    monkeypatch.setattr(lifecycle, "command", cast("Callable[..., object]", command))

    # When
    created = lifecycle.create_bot("demo")

    # Then
    assert created["name"] == "demo"
    assert calls[-1] == (
        "updateAgent",
        {
            "id": "new-1",
            "profile": {"name": "demo", "description": WORKER_DESCRIPTION},
        },
    )


def test_provision_requires_existing_name_before_update(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    lifecycle = manager()
    mutations = 0

    def command(_method: str, _args: dict[str, object] | None = None) -> object:
        nonlocal mutations
        mutations += 1
        return None

    monkeypatch.setattr(lifecycle, "command", cast("Callable[..., object]", command))

    provision_name = "_provision_bot"
    provision = cast(
        "Callable[[dict[str, object], str | None], dict[str, object]]",
        getattr(lifecycle, provision_name),
    )

    # When / Then
    with pytest.raises(ConnectError, match="missing name"):
        _ = provision({"id": "agent-1", "description": "old"}, None)
    assert mutations == 0
