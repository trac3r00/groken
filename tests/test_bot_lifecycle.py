from __future__ import annotations

import json
import sys
from typing import Protocol, cast, final

import httpx
import pytest

import groken.gateway as gateway_module
from groken import cli, mcp_server
from groken.client import ConnectError
from groken.gateway import GatewayManager
from groken.provisioning import WORKER_DESCRIPTION

CommandCall = tuple[str, dict[str, object] | None]


@final
class _Session:
    def __init__(self, result: object | BaseException) -> None:
        self.result = result
        self.calls: list[CommandCall] = []

    def command(self, method: str, args: dict[str, object] | None = None) -> object:
        self.calls.append((method, None if args is None else dict(args)))
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


class _LifecycleManager(Protocol):
    def create_bot(self, name: str) -> dict[str, object]: ...
    def duplicate_bot(self, source_name: str, name: str) -> dict[str, object]: ...


def _lifecycle_manager() -> _LifecycleManager:
    return cast(_LifecycleManager, cast(object, GatewayManager.__new__(GatewayManager)))


def test_create_bot_reuses_exact_nonce_and_body_after_remint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _Session(httpx.ReadError("connection reset after request"))
    second = _Session(
        {"agent": {"id": "new-1", "name": "demo", "description": WORKER_DESCRIPTION}}
    )
    forces: list[bool] = []
    manager = _lifecycle_manager()

    def session(force: bool = False) -> _Session:
        forces.append(force)
        return second if force else first

    monkeypatch.setattr(manager, "session", session)

    created = manager.create_bot("demo")

    assert created["id"] == "new-1"
    assert forces == [False, True]
    assert first.calls == second.calls
    method, body = first.calls[0]
    assert method == "createAgent"
    assert body is not None
    assert body == {
        "name": "demo",
        "description": WORKER_DESCRIPTION,
        "title": "groken bridge worker",
        "clientNonce": body["clientNonce"],
    }


def test_create_bot_does_not_retry_semantic_500(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failed = _Session(ConnectError(500, "duplicate name"))
    forces: list[bool] = []
    manager = _lifecycle_manager()

    def session(force: bool = False) -> _Session:
        forces.append(force)
        return failed

    monkeypatch.setattr(manager, "session", session)

    with pytest.raises(ConnectError, match="duplicate name"):
        _ = manager.create_bot("demo")

    assert forces == [False]
    assert len(failed.calls) == 1


def test_create_bot_applies_guardrail_once_and_uses_distinct_logical_nonces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[CommandCall] = []
    next_id = iter(("one", "two"))
    manager = _lifecycle_manager()

    def command(method: str, args: dict[str, object] | None = None) -> object:
        calls.append((method, args))
        if method == "createAgent":
            assert args is not None
            return {"agent": {"id": next(next_id), "name": args["name"]}}
        return None

    monkeypatch.setattr(manager, "command", command)

    assert manager.create_bot("alpha")["description"] == WORKER_DESCRIPTION
    assert manager.create_bot("beta")["description"] == WORKER_DESCRIPTION

    creates = [
        body for method, body in calls if method == "createAgent" and body is not None
    ]
    updates = [
        body for method, body in calls if method == "updateAgent" and body is not None
    ]
    assert creates[0]["clientNonce"] != creates[1]["clientNonce"]
    assert updates == [
        {
            "id": "one",
            "profile": {"name": "alpha", "description": WORKER_DESCRIPTION},
        },
        {
            "id": "two",
            "profile": {"name": "beta", "description": WORKER_DESCRIPTION},
        },
    ]


def test_duplicate_bot_uses_verified_contract_then_guardrail_update(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[CommandCall] = []
    manager = _lifecycle_manager()

    def command(method: str, args: dict[str, object] | None = None) -> object:
        calls.append((method, args))
        if method == "listAgents":
            return [{"id": "source-id", "name": "source"}]
        if method == "duplicateAgent":
            return {"agent": {"id": "copy-id", "name": "source copy"}}
        return {"id": "copy-id", "name": "new", "description": WORKER_DESCRIPTION}

    monkeypatch.setattr(manager, "command", command)

    duplicated = manager.duplicate_bot("source", "new")

    assert duplicated == {
        "id": "copy-id",
        "name": "new",
        "description": WORKER_DESCRIPTION,
    }
    assert calls == [
        ("listAgents", None),
        ("duplicateAgent", {"id": "source-id"}),
        (
            "updateAgent",
            {
                "id": "copy-id",
                "profile": {"name": "new", "description": WORKER_DESCRIPTION},
            },
        ),
    ]


def test_duplicate_transport_reset_is_indeterminate_and_not_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reset = _Session(httpx.ReadError("connection reset after request"))
    manager = _lifecycle_manager()

    def command(method: str, args: dict[str, object] | None = None) -> object:
        if method == "listAgents":
            return [{"id": "source-id", "name": "source"}]
        return GatewayManager.command(cast(GatewayManager, manager), method, args)

    monkeypatch.setattr(manager, "command", command)
    monkeypatch.setattr(manager, "session", lambda force=False: reset)

    with pytest.raises(ConnectError, match="outcome indeterminate"):
        _ = manager.duplicate_bot("source", "new")

    assert reset.calls == [("duplicateAgent", {"id": "source-id"})]


def test_duplicate_remints_after_auth_rejection_with_same_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _Session(ConnectError(401, "expired"))
    second = _Session(
        {"agent": {"id": "copy-id", "name": "new", "description": WORKER_DESCRIPTION}}
    )
    manager = _lifecycle_manager()

    def command(method: str, args: dict[str, object] | None = None) -> object:
        if method == "listAgents":
            return [{"id": "source-id", "name": "source"}]
        return GatewayManager.command(cast(GatewayManager, manager), method, args)

    monkeypatch.setattr(manager, "command", command)
    monkeypatch.setattr(
        manager, "session", lambda force=False: second if force else first
    )

    assert manager.duplicate_bot("source", "new")["id"] == "copy-id"
    assert first.calls == second.calls == [("duplicateAgent", {"id": "source-id"})]


@pytest.mark.parametrize("name", ["", "   "])
def test_create_bot_rejects_empty_name_before_mutation(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    manager = _lifecycle_manager()

    def unexpected_command(
        method: str, args: dict[str, object] | None = None
    ) -> object:
        _ = args
        pytest.fail(method)

    monkeypatch.setattr(manager, "command", unexpected_command)

    with pytest.raises(ConnectError, match="must not be empty"):
        _ = manager.create_bot(name)


def test_duplicate_unknown_source_has_no_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[CommandCall] = []
    manager = _lifecycle_manager()

    def command(method: str, args: dict[str, object] | None = None) -> object:
        calls.append((method, args))
        return []

    monkeypatch.setattr(manager, "command", command)
    with pytest.raises(ConnectError, match="unknown source"):
        _ = manager.duplicate_bot("missing", "new")
    assert calls == [("listAgents", None)]


def test_create_bot_accepts_max_length_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[CommandCall] = []
    manager = _lifecycle_manager()
    max_length = gateway_module.MAX_BOT_NAME_LENGTH

    def command(method: str, args: dict[str, object] | None = None) -> object:
        calls.append((method, args))
        if method == "createAgent":
            return {"agent": {"id": "new-1", "name": "x" * max_length}}
        return None

    monkeypatch.setattr(manager, "command", command)

    created = manager.create_bot("x" * max_length)

    assert created["name"] == "x" * max_length
    creates = [body for method, body in calls if method == "createAgent"]
    assert len(creates) == 1
    assert creates[0] is not None
    assert creates[0]["name"] == "x" * max_length


def test_create_bot_rejects_oversized_name_before_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _lifecycle_manager()

    def unexpected_command(
        method: str, args: dict[str, object] | None = None
    ) -> object:
        _ = args
        pytest.fail(method)

    monkeypatch.setattr(manager, "command", unexpected_command)

    with pytest.raises(ConnectError, match="at most"):
        _ = manager.create_bot("x" * (gateway_module.MAX_BOT_NAME_LENGTH + 1))


def test_duplicate_bot_rejects_oversized_new_name_before_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _lifecycle_manager()

    def unexpected_command(
        method: str, args: dict[str, object] | None = None
    ) -> object:
        _ = args
        pytest.fail(method)

    monkeypatch.setattr(manager, "command", unexpected_command)

    with pytest.raises(ConnectError, match="at most"):
        _ = manager.duplicate_bot(
            "source", "x" * (gateway_module.MAX_BOT_NAME_LENGTH + 1)
        )


def test_duplicate_bot_accepts_long_source_id(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[CommandCall] = []
    manager = _lifecycle_manager()
    long_id = "i" * 310

    def command(method: str, args: dict[str, object] | None = None) -> object:
        calls.append((method, args))
        if method == "listAgents":
            return [{"id": long_id, "name": "source"}]
        if method == "duplicateAgent":
            return {"agent": {"id": "copy-id", "name": "source copy"}}
        return {"id": "copy-id"}

    monkeypatch.setattr(manager, "command", command)

    duplicated = manager.duplicate_bot(long_id, "new")

    assert duplicated["id"] == "copy-id"
    assert calls[:2] == [
        ("listAgents", None),
        ("duplicateAgent", {"id": long_id}),
    ]


@pytest.mark.parametrize("source", ["", "   "])
def test_duplicate_bot_rejects_empty_source_before_list(
    monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    manager = _lifecycle_manager()

    def unexpected_command(
        method: str, args: dict[str, object] | None = None
    ) -> object:
        _ = args
        pytest.fail(method)

    monkeypatch.setattr(manager, "command", unexpected_command)

    with pytest.raises(ConnectError, match="source bot must not be empty"):
        _ = manager.duplicate_bot(source, "new")


def test_bot_cli_group_dispatches_add_and_preserves_top_level_verbs(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class Manager:
        def create_bot(self, name: str) -> dict[str, object]:
            return {"id": "new-id", "name": name, "description": WORKER_DESCRIPTION}

    monkeypatch.setattr(cli, "_manager", Manager)
    monkeypatch.setattr(sys, "argv", ["groken", "bot", "add", "demo"])

    cli.main()

    assert capsys.readouterr().out.strip() == "new-id  demo"

    monkeypatch.setattr(sys, "argv", ["groken", "bot", "--help"])
    with pytest.raises(SystemExit, match="0"):
        cli.main()
    bot_help = capsys.readouterr().out
    assert "add" in bot_help and "duplicate" in bot_help and "delete" not in bot_help

    monkeypatch.setattr(sys, "argv", ["groken", "--help"])
    with pytest.raises(SystemExit, match="0"):
        cli.main()
    root_help = capsys.readouterr().out
    assert all(verb in root_help for verb in ("bots", "list", "configure", "routine"))


def test_cli_duplicate_name_error_is_nonzero_and_single_mutation(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    mutations = 0

    class Manager:
        def duplicate_bot(self, source_name: str, name: str) -> dict[str, object]:
            nonlocal mutations
            mutations += 1
            raise ConnectError(500, f"duplicate name: {name} from {source_name}")

    monkeypatch.setattr(cli, "_manager", Manager)
    monkeypatch.setattr(sys, "argv", ["groken", "bot", "duplicate", "source", "taken"])

    with pytest.raises(SystemExit, match="1"):
        cli.main()

    assert mutations == 1
    assert "Gateway error 500" in capsys.readouterr().err


@pytest.mark.anyio
async def test_mcp_lifecycle_tools_are_confirmed_registered_and_non_destructive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, ...]] = []

    class Manager:
        def create_bot(self, name: str) -> dict[str, object]:
            calls.append(("add", name))
            return {"id": "new-id", "name": name}

        def duplicate_bot(self, source_name: str, name: str) -> dict[str, object]:
            calls.append(("duplicate", source_name, name))
            return {"id": "copy-id", "name": name}

    monkeypatch.setattr(mcp_server, "GatewayManager", Manager)

    assert "confirmed=true" in mcp_server.grok_bot_add("demo", confirmed=False)
    assert "confirmed=true" in mcp_server.grok_bot_duplicate(
        "source", "copy", confirmed=False
    )
    assert calls == []
    assert json.loads(mcp_server.grok_bot_add("demo", confirmed=True))["id"] == "new-id"
    assert (
        json.loads(mcp_server.grok_bot_duplicate("source", "copy", confirmed=True))[
            "id"
        ]
        == "copy-id"
    )
    assert calls == [("add", "demo"), ("duplicate", "source", "copy")]

    tools = await mcp_server.server.list_tools()
    lifecycle = {tool.name: tool.description for tool in tools if "bot_" in tool.name}
    assert lifecycle["grok_bot_add"]
    assert lifecycle["grok_bot_duplicate"]
    assert not any("delete" in name for name in lifecycle)
