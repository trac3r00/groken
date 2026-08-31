from __future__ import annotations

import multiprocessing
from typing import Protocol, TypeAlias, final

import pytest

from groken import swarm

TASK = "summarize the repo"

JsonValue: TypeAlias = (
    None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]
)


class ReplyCallable(Protocol):
    def __call__(self) -> str: ...


Reply: TypeAlias = str | BaseException | ReplyCallable
ScriptedReply: TypeAlias = Reply | list[Reply]


@final
class FakeManager:
    """Fake GatewayManager: scripted per-bot replies plus a call log."""

    def __init__(
        self,
        replies: dict[str, ScriptedReply],
        agents: list[dict[str, JsonValue]] | None = None,
        sharing: JsonValue = None,
    ) -> None:
        self.replies = replies
        self.agents: list[dict[str, JsonValue]] = agents or [
            {"id": f"{name}-id", "name": name, "isRunning": True} for name in replies
        ]
        self.sharing = sharing
        self.commands: list[tuple[str, dict[str, JsonValue] | None]] = []
        context = multiprocessing.get_context("fork")
        self._ask_receive, self._ask_send = context.Pipe(duplex=False)
        self._ask_lock = context.Lock()
        self._asks: list[tuple[str, str, float]] = []
        self._counts = {name: context.Value("i", 0) for name in replies}

    @property
    def asks(self) -> list[tuple[str, str, float]]:
        while self._ask_receive.poll():
            self._asks.append(self._ask_receive.recv())
        return self._asks

    def command(
        self, method: str, args: dict[str, JsonValue] | None = None
    ) -> JsonValue:
        self.commands.append((method, args))
        if method == "listAgents":
            return list(self.agents)
        if method == "getSharingState":
            return self.sharing
        raise AssertionError(f"unexpected command {method}")

    def ask(self, agent_id: str, text: str, timeout_s: float = 600) -> str:
        with self._ask_lock:
            self._ask_send.send((agent_id, text, timeout_s))
        name = agent_id.removesuffix("-id")
        counter = self._counts[name]
        with counter.get_lock():
            index = counter.value
            counter.value += 1
        scripted = self.replies[name]
        if isinstance(scripted, list):
            scripted = scripted[min(index, len(scripted) - 1)]
        if isinstance(scripted, BaseException):
            raise scripted
        if callable(scripted):
            return str(scripted())
        return str(scripted)


def bots(*names: str) -> list[str]:
    return list(names)


def test_sections_follow_requested_order_not_completion_order() -> None:
    # Given three bots whose replies complete in reverse order
    context = multiprocessing.get_context("fork")
    done = {name: context.Event() for name in ("a", "b", "c")}

    def reply(name: str, waits_for: str | None) -> ReplyCallable:
        def run() -> str:
            if waits_for is not None:
                assert done[waits_for].wait(5)
            done[name].set()
            return f"{name} answer"

        return run

    manager = FakeManager(
        {"a": reply("a", "b"), "b": reply("b", "c"), "c": reply("c", None)}
    )

    # When
    outcome = swarm.run_swarm(manager, swarm.SwarmRequest(bots("a", "b", "c"), TASK))

    # Then
    assert all(event.is_set() for event in done.values())
    rendered = swarm.render(outcome)
    assert [line for line in rendered.splitlines() if line.startswith("=== ")] == [
        "=== a ===",
        "=== b ===",
        "=== c ===",
    ]
    assert "a answer" in rendered
    assert outcome.exit_code == 0


def test_one_failing_bot_keeps_other_sections_and_exit_zero() -> None:
    # Given
    manager = FakeManager(
        {"a": "a answer", "b": RuntimeError("gateway exploded"), "c": "c answer"}
    )

    # When
    outcome = swarm.run_swarm(manager, swarm.SwarmRequest(bots("a", "b", "c"), TASK))

    # Then
    rendered = swarm.render(outcome)
    assert "a answer" in rendered
    assert "c answer" in rendered
    assert "FAILED: gateway exploded" in rendered
    assert "RuntimeError:" not in rendered
    assert outcome.exit_code == 0


def test_all_bots_failing_exits_one() -> None:
    # Given
    manager = FakeManager({"a": RuntimeError("down"), "b": TimeoutError("slow")})

    # When
    outcome = swarm.run_swarm(manager, swarm.SwarmRequest(bots("a", "b"), TASK))

    # Then
    assert outcome.exit_code == 1
    rendered = swarm.render(outcome)
    assert rendered.count("FAILED") == 2
    assert "down" in rendered
    assert "slow" in rendered


def test_omitted_bots_uses_roster_and_honors_exclude() -> None:
    # Given a roster with the configured default plus two peers
    manager = FakeManager({"groken": "hi", "alpha": "hey", "beta": "yo"})

    # When
    outcome = swarm.run_swarm(manager, swarm.SwarmRequest(None, TASK, exclude=["beta"]))

    # Then
    assert [section.bot for section in outcome.sections] == ["groken", "alpha"]
    assert sorted(agent for agent, _, _ in manager.asks) == ["alpha-id", "groken-id"]


def test_duplicate_and_unknown_selections_are_rejected_before_sending() -> None:
    # Given
    manager = FakeManager({"a": "a answer"})

    # When / Then
    with pytest.raises(swarm.SwarmSelectionError, match="duplicate"):
        _ = swarm.run_swarm(manager, swarm.SwarmRequest(bots("a", "a"), TASK))
    with pytest.raises(swarm.SwarmSelectionError, match="unknown bot"):
        _ = swarm.run_swarm(manager, swarm.SwarmRequest(bots("a", "ghost"), TASK))
    with pytest.raises(swarm.SwarmSelectionError, match="no bots"):
        _ = swarm.run_swarm(manager, swarm.SwarmRequest(bots("  "), TASK))
    assert manager.asks == []


def test_each_bot_receives_its_own_bounded_timeout() -> None:
    # Given
    manager = FakeManager({"a": "a answer", "b": "b answer"})

    # When
    _ = swarm.run_swarm(manager, swarm.SwarmRequest(bots("a", "b"), TASK, timeout_s=12))

    # Then
    assert sorted(manager.asks) == [
        ("a-id", TASK, 12.0),
        ("b-id", TASK, 12.0),
    ]


def test_second_round_relays_peer_replies_as_delimited_data() -> None:
    # Given
    manager = FakeManager(
        {"a": ["a first", "a second"], "b": ["b first", RuntimeError("b broke")]}
    )

    # When
    outcome = swarm.run_swarm(
        manager, swarm.SwarmRequest(bots("a", "b"), TASK, rounds=2)
    )

    # Then
    second_round = [call for call in manager.asks if call[1] != TASK]
    assert len(second_round) == 2
    for _, prompt, _timeout in second_round:
        assert TASK in prompt
        assert swarm.PEER_BLOCK_START in prompt
        assert swarm.PEER_BLOCK_END in prompt
        assert "untrusted data" in prompt.lower()
    prompt_for_a = next(prompt for agent, prompt, _ in second_round if agent == "a-id")
    assert "b first" in prompt_for_a
    assert "a first" not in prompt_for_a.split(swarm.PEER_BLOCK_START)[1]
    rendered = swarm.render(outcome)
    assert "a second" in rendered
    assert "FAILED" in rendered


def test_relayed_peer_payload_is_bounded() -> None:
    # Given an enormous first-round reply
    flood = "x" * (swarm.MAX_PEER_PAYLOAD_CHARS * 3)
    manager = FakeManager({"a": ["a first", "a second"], "b": [flood, "b second"]})

    # When
    _ = swarm.run_swarm(manager, swarm.SwarmRequest(bots("a", "b"), TASK, rounds=2))

    # Then
    prompt_for_a = next(
        prompt
        for agent, prompt, _ in manager.asks
        if agent == "a-id" and prompt != TASK
    )
    assert len(prompt_for_a) <= len(TASK) + swarm.MAX_PEER_PAYLOAD_CHARS + 2000
    assert swarm.TRUNCATION_MARK in prompt_for_a


def test_rounds_are_bounded_to_three() -> None:
    # Given
    manager = FakeManager({"a": "a answer"})

    # When / Then
    with pytest.raises(swarm.SwarmSelectionError, match="rounds"):
        _ = swarm.run_swarm(manager, swarm.SwarmRequest(bots("a"), TASK, rounds=4))


def test_swarm_leaves_no_worker_processes_behind() -> None:
    # Given
    before = {process.pid for process in multiprocessing.active_children()}
    manager = FakeManager({"a": "a answer", "b": "b answer", "c": "c answer"})

    # When
    outcome = swarm.run_swarm(manager, swarm.SwarmRequest(bots("a", "b", "c"), TASK))

    # Then
    assert outcome.exit_code == 0
    assert {process.pid for process in multiprocessing.active_children()} <= before
