from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol, final

from .env_collectors import (
    CollectedEnvironment,
    CollectorOutput,
    CollectorStatus,
    CommandRequest,
    CommandResult,
    Inventory,
    NativeAdapterError,
    NativePlaneUnavailable,
    NativeRunner,
    collect_environment,
)
from .env_native_runner import NativeEnvironmentRunner
from .env_persistence import (
    ManifestTree,
    MirrorTarget,
    PersistenceError,
    TreeFile,
    canonical_json_bytes,
    content_id,
    mirror_tree,
    validate_component,
)
from .native_client import NativeControllerClient
from .native_wait_models import NativeClientConfigurationError


@dataclass(frozen=True, slots=True)
class BotIdentity:
    id: str
    name: str


@dataclass(frozen=True, slots=True)
class CaptureConfig:
    bot: BotIdentity
    local_root: Path
    captured_at: Callable[[], datetime]


class CaptureSource(StrEnum):
    NATIVE = "native"
    CHAT = "chat"


@dataclass(frozen=True, slots=True)
class CaptureOutcome:
    manifest_id: str
    local_path: Path
    source: CaptureSource = CaptureSource.NATIVE


class CaptureError(Exception):
    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class ChatCollector(Protocol):
    def collect(self, bot: BotIdentity) -> str: ...


class ChatGateway(Protocol):
    def ask(self, agent_id: str, text: str, timeout_s: float = 600) -> str: ...


class Gateway(ChatGateway, Protocol):
    def command(self, method: str, args: dict[str, object] | None = None) -> object: ...
    def resolve_agent(self, bot: str | None = None) -> str: ...


class GatewayChatCollector:
    def __init__(self, manager: ChatGateway) -> None:
        self._manager = manager

    def collect(self, bot: BotIdentity) -> str:
        prompt = (
            "Read the computer environment without installing or changing anything. "
            "Return only one JSON object with exact keys host and inventory. host must "
            "contain os, os_version, arch. inventory must contain brewfile, python, npm, "
            "pipx, mas, applications; npm must contain node_version, prefix, packages."
        )
        return self._manager.ask(bot.id, prompt, timeout_s=600)


@final
class _UnavailableNativeRunner:
    def __init__(self, detail: str) -> None:
        self._detail = detail

    def run(self, request: CommandRequest) -> CommandResult:
        del request
        raise NativePlaneUnavailable(self._detail)

    def publish(self, tree: ManifestTree) -> None:
        del tree
        raise NativePlaneUnavailable(self._detail)


def _inventory_payload(inventory: Inventory) -> dict[str, object]:
    return {
        "brewfile": inventory.brewfile,
        "python": list(inventory.python),
        "npm": inventory.npm,
        "pipx": list(inventory.pipx),
        "mas": list(inventory.mas),
        "applications": list(inventory.applications),
    }


def _collector_payload(row: CollectorOutput, artifact: str) -> dict[str, object]:
    return {
        "id": row.id,
        "status": row.status.value,
        "artifact": artifact,
        "sha256": hashlib.sha256(row.artifact).hexdigest(),
        "command": list(row.command),
        "exit_code": row.exit_code,
        "error": row.error,
    }


def _base_payload(
    config: CaptureConfig, collected: CollectedEnvironment
) -> tuple[dict[str, object], tuple[TreeFile, ...]]:
    artifacts = tuple(
        TreeFile(f"artifacts/{row.id}.raw", row.artifact)
        for row in sorted(collected.collectors, key=lambda item: item.id)
    )
    artifact_paths = {path.path for path in artifacts}
    collectors = [
        _collector_payload(row, f"artifacts/{row.id}.raw")
        for row in collected.collectors
        if f"artifacts/{row.id}.raw" in artifact_paths
    ]
    captured = config.captured_at().astimezone(UTC).isoformat().replace("+00:00", "Z")
    return {
        "schema_version": 1,
        "bot": {"id": config.bot.id, "name": config.bot.name},
        "captured_at": captured,
        "host": collected.host,
        "collectors": collectors,
        "inventory": _inventory_payload(collected.inventory),
    }, artifacts


def _string_map(value: object, keys: set[str]) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != keys:
        raise CaptureError("chat collector returned an invalid object")
    parsed: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, str):
            raise CaptureError("chat collector returned a non-string field")
        parsed[key] = item
    return parsed


def _string_rows(value: object, keys: set[str]) -> tuple[dict[str, str], ...]:
    if not isinstance(value, list):
        raise CaptureError("chat collector returned an invalid list")
    rows: list[dict[str, str]] = []
    for item in value:
        if (
            not isinstance(item, dict)
            or set(item) != keys
            or not all(
                isinstance(key, str) and isinstance(field, str)
                for key, field in item.items()
            )
        ):
            raise CaptureError("chat collector returned an invalid list row")
        rows.append(dict(item))
    return tuple(rows)


def _chat_environment(raw: str, native_error: str) -> CollectedEnvironment:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CaptureError(f"chat collector returned invalid JSON: {exc}") from exc
    if not isinstance(payload, dict) or set(payload) != {"host", "inventory"}:
        raise CaptureError("chat collector returned invalid top-level keys")
    host = _string_map(payload["host"], {"os", "os_version", "arch"})
    inventory = payload["inventory"]
    expected = {"brewfile", "python", "npm", "pipx", "mas", "applications"}
    if not isinstance(inventory, dict) or set(inventory) != expected:
        raise CaptureError("chat collector returned invalid inventory keys")
    brewfile = inventory["brewfile"]
    python = inventory["python"]
    npm = inventory["npm"]
    if (
        not isinstance(brewfile, str)
        or not isinstance(python, list)
        or not isinstance(npm, dict)
    ):
        raise CaptureError("chat collector returned invalid inventory values")
    if set(npm) != {"node_version", "prefix", "packages"}:
        raise CaptureError("chat collector returned invalid npm keys")
    node_version, prefix, packages = npm["node_version"], npm["prefix"], npm["packages"]
    if not isinstance(node_version, str) or not isinstance(prefix, str):
        raise CaptureError("chat collector returned invalid npm values")
    python_rows: list[dict[str, str | list[str]]] = []
    for row in python:
        if not isinstance(row, dict) or set(row) != {
            "scope",
            "executable",
            "version",
            "requirements",
        }:
            raise CaptureError("chat collector returned invalid python rows")
        requirements = row["requirements"]
        if (
            not all(
                isinstance(row[key], str) for key in ("scope", "executable", "version")
            )
            or not isinstance(requirements, list)
            or not all(isinstance(item, str) for item in requirements)
        ):
            raise CaptureError("chat collector returned invalid python values")
        python_rows.append(
            {
                "scope": row["scope"],
                "executable": row["executable"],
                "version": row["version"],
                "requirements": list(requirements),
            }
        )
    parsed_inventory = Inventory(
        brewfile,
        tuple(python_rows),
        {
            "node_version": node_version,
            "prefix": prefix,
            "packages": list(_string_rows(packages, {"name", "version"})),
        },
        _string_rows(inventory["pipx"], {"name", "version"}),
        _string_rows(inventory["mas"], {"id", "name", "version"}),
        _string_rows(
            inventory["applications"], {"name", "path", "bundle_id", "version"}
        ),
    )
    collector = CollectorOutput(
        "chat", CollectorStatus.PARTIAL, raw.encode(), ("chat",), None, native_error
    )
    return CollectedEnvironment(host, (collector,), parsed_inventory)


def parse_chat_inventory(raw: str) -> Inventory:
    """Parse the strict task-4 chat inventory contract without persisting it."""
    return _chat_environment(raw, "native restore inventory unavailable").inventory


def capture_environment(
    config: CaptureConfig, runner: NativeRunner, chat: ChatCollector | None = None
) -> CaptureOutcome:
    try:
        validate_component(config.bot.id)
    except PersistenceError as exc:
        raise CaptureError(str(exc)) from exc
    source = CaptureSource.NATIVE
    try:
        collected = collect_environment(runner)
        payload, artifacts = _base_payload(config, collected)
        manifest_id = content_id(payload, artifacts)
        final_payload = {**payload, "manifest_id": manifest_id}
        tree = ManifestTree(
            manifest_id,
            (
                *artifacts,
                TreeFile("manifest.json", canonical_json_bytes(final_payload)),
            ),
        )
        runner.publish(tree)
    except NativeAdapterError as exc:
        raise CaptureError(f"native capture failed: {exc}") from exc
    except NativePlaneUnavailable as exc:
        source = CaptureSource.CHAT
        if chat is None:
            raise CaptureError(f"native capture failed: {exc}") from exc
        collected = _chat_environment(chat.collect(config.bot), str(exc))
        payload, artifacts = _base_payload(config, collected)
        manifest_id = content_id(payload, artifacts)
        final_payload = {**payload, "manifest_id": manifest_id}
        tree = ManifestTree(
            manifest_id,
            (
                *artifacts,
                TreeFile("manifest.json", canonical_json_bytes(final_payload)),
            ),
        )
    except Exception as exc:  # WHY: adapter bugs are translated, never treated as native unavailability.
        raise CaptureError(f"native capture failed: {exc}") from exc
    try:
        local_path = mirror_tree(MirrorTarget(config.local_root, config.bot.id), tree)
    except PersistenceError as exc:
        raise CaptureError(str(exc)) from exc
    return CaptureOutcome(tree.manifest_id, local_path, source)


def capture_for_gateway(manager: Gateway, bot: str | None = None) -> CaptureOutcome:
    bot_id = manager.resolve_agent(bot)
    roster = manager.command("listAgents")
    name = bot or bot_id
    if isinstance(roster, list):
        for item in roster:
            if isinstance(item, dict) and item.get("id") == bot_id:
                raw_name = item.get("name")
                name = raw_name if isinstance(raw_name, str) else name
                break
    config = CaptureConfig(
        BotIdentity(bot_id, name),
        Path.home() / ".config/groken/env",
        lambda: datetime.now(UTC),
    )
    chat = GatewayChatCollector(manager)
    try:
        client = NativeControllerClient()
    except NativeClientConfigurationError as exc:
        return capture_environment(config, _UnavailableNativeRunner(str(exc)), chat)
    environment = NativeEnvironmentRunner(client)
    try:
        return capture_environment(config, environment.task4_runner(bot_id), chat)
    finally:
        environment.close()
