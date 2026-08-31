from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import final

import pytest

from groken import env_manifest
from groken.env_collectors import (
    CommandRequest,
    CommandResult,
    NativePlaneUnavailable,
    NativeRunner,
)
from groken.env_manifest import (
    BotIdentity,
    CaptureConfig,
    CaptureError,
    CaptureOutcome,
    CaptureSource,
    ChatCollector,
    GatewayChatCollector,
    capture_environment,
)
from groken.env_native_runner import NativeAdapterError
from groken.env_persistence import ManifestTree
from groken.native_client import NativeControllerClient
from groken.native_wait_models import NativeClientConfigurationError


@final
class Gateway:
    def resolve_agent(self, bot: str | None = None) -> str:
        return "bot-1" if bot is None else bot

    def command(self, method: str, args: dict[str, object] | None = None) -> object:
        assert method == "listAgents" and args is None
        return [{"id": "bot-1", "name": "Demo"}]

    def ask(self, agent_id: str, text: str, timeout_s: float = 600) -> str:
        raise AssertionError((agent_id, text, timeout_s))


@final
class TaskRunner:
    def run(self, request: CommandRequest) -> CommandResult:
        raise AssertionError(request)

    def publish(self, tree: ManifestTree) -> None:
        raise AssertionError(tree)


@final
class AdapterFailureRunner:
    def run(self, request: CommandRequest) -> CommandResult:
        raise NativeAdapterError(f"adapter failed for {request.argv[0]}")

    def publish(self, tree: ManifestTree) -> None:
        raise NativeAdapterError(f"adapter failed for {tree.manifest_id}")


@final
class RejectingChat:
    def __init__(self) -> None:
        self.calls = 0

    def collect(self, bot: BotIdentity) -> str:
        self.calls += 1
        raise AssertionError(bot)


@final
class Client:
    def __init__(self) -> None:
        self.closed = False


@final
class Environment:
    def __init__(self, client: Client, task_runner: NativeRunner) -> None:
        self.client = client
        self.task_runner = task_runner
        self.namespaces: list[str] = []
        self.closed = False

    def task4_runner(self, namespace: str) -> NativeRunner:
        self.namespaces.append(namespace)
        return self.task_runner

    def close(self) -> None:
        self.closed = self.client.closed = True


def test_capture_environment_keeps_native_adapter_failures_out_of_chat_fallback(
    tmp_path: Path,
) -> None:
    # Given
    chat = RejectingChat()
    config = CaptureConfig(
        BotIdentity("bot-1", "Demo"),
        tmp_path / "env",
        lambda: datetime(2026, 8, 27, tzinfo=UTC),
    )

    # When / Then
    with pytest.raises(CaptureError, match="adapter failed"):
        _ = capture_environment(config, AdapterFailureRunner(), chat)
    assert chat.calls == 0


def test_capture_for_gateway_uses_task4_runner_and_closes_native_resources(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # Given
    client = Client()
    task_runner = TaskRunner()
    environment = Environment(client, task_runner)
    expected = CaptureOutcome("sha256:fixture", tmp_path / "manifest")

    def make_client() -> Client:
        return client

    def make_environment(created: Client) -> Environment:
        assert created is client
        return environment

    def capture(
        config: CaptureConfig,
        runner: NativeRunner,
        chat: ChatCollector | None = None,
    ) -> CaptureOutcome:
        assert config.bot == BotIdentity("bot-1", "Demo")
        assert runner is task_runner
        assert isinstance(chat, GatewayChatCollector)
        return expected

    monkeypatch.setattr(env_manifest, "NativeControllerClient", make_client)
    monkeypatch.setattr(env_manifest, "NativeEnvironmentRunner", make_environment)
    monkeypatch.setattr(env_manifest, "capture_environment", capture)

    # When
    outcome = env_manifest.capture_for_gateway(Gateway())

    # Then
    assert outcome == expected
    assert environment.namespaces == ["bot-1"]
    assert environment.closed and client.closed


def test_capture_for_gateway_closes_native_resources_when_capture_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    client = Client()
    environment = Environment(client, TaskRunner())

    def make_client() -> Client:
        return client

    def make_environment(created: Client) -> Environment:
        assert created is client
        return environment

    def fail_capture(
        config: CaptureConfig,
        runner: NativeRunner,
        chat: ChatCollector | None = None,
    ) -> CaptureOutcome:
        del config, runner, chat
        raise CaptureError("persistence bug")

    monkeypatch.setattr(env_manifest, "NativeControllerClient", make_client)
    monkeypatch.setattr(env_manifest, "NativeEnvironmentRunner", make_environment)
    monkeypatch.setattr(env_manifest, "capture_environment", fail_capture)

    # When / Then
    with pytest.raises(CaptureError, match="persistence bug"):
        _ = env_manifest.capture_for_gateway(Gateway())
    assert environment.closed and client.closed


def test_capture_for_gateway_falls_back_only_when_native_configuration_is_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # Given
    expected = CaptureOutcome("sha256:chat", tmp_path / "manifest", CaptureSource.CHAT)

    def missing_client() -> NativeControllerClient:
        raise NativeClientConfigurationError("controller token missing")

    def capture(
        config: CaptureConfig,
        runner: NativeRunner,
        chat: ChatCollector | None = None,
    ) -> CaptureOutcome:
        del config
        with pytest.raises(NativePlaneUnavailable, match="controller token missing"):
            _ = runner.run(CommandRequest(("/usr/bin/uname", "-s")))
        assert isinstance(chat, GatewayChatCollector)
        return expected

    monkeypatch.setattr(env_manifest, "NativeControllerClient", missing_client)
    monkeypatch.setattr(env_manifest, "capture_environment", capture)

    # When
    outcome = env_manifest.capture_for_gateway(Gateway())

    # Then
    assert outcome is expected
