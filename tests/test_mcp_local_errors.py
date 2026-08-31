from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import pytest
from mcp.types import CallToolResult

import groken.mcp_operations as operations
import groken.mcp_server as m
from groken.auth import TokenStateError
from groken.config import ConfigStateError
from groken.env_collectors import NativePlaneUnavailable
from groken.env_manifest import CaptureError
from groken.env_persistence import PersistenceError
from groken.env_restore_errors import JournalConflictError, JournalUnsafeError
from groken.env_restore_gateway import RestoreCommandOptions
from groken.env_restore_manifest import RestoreManifestError
from groken.env_restore_run import RestorePendingError
from groken.env_restore_validation import RestoreInputError
from groken.gateway import BotUpdateError
from groken.mcp_support import translate_async_tool_errors, translate_tool_errors
from groken.routines import RoutineError
from groken.swarm_rooms import SwarmError
from groken.swarm_worker import WorkerProtocolError

HOME_SEGMENT = "mcp-private-home"
BOT_ID = "bot-private-7f02"
MANIFEST_ID = f"sha256:{'a' * 64}"
JOURNAL_ID = "journal-private-91bd"
CONTROLLER_URL = "http://127.0.0.1:9471/internal/controller-private"
ABSOLUTE_PATH = (
    f"/Users/{HOME_SEGMENT}/.config/groken/env/{BOT_ID}/{MANIFEST_ID}/"
    f"journal/{JOURNAL_ID}.json"
)
PRIVATE_DETAIL = (
    f"unsafe local state at {ABSOLUTE_PATH}; controller={CONTROLLER_URL}; "
    f"bot={BOT_ID}; manifest={MANIFEST_ID}; journal={JOURNAL_ID}"
)


@dataclass(frozen=True, slots=True)
class LocalErrorCase:
    name: str
    factory: Callable[[str], Exception]


LOCAL_ERROR_CASES = (
    LocalErrorCase("tokens", lambda detail: TokenStateError(Path(detail), "malformed JSON")),
    LocalErrorCase(
        "config",
        lambda detail: ConfigStateError(Path(detail), "expected a JSON object"),
    ),
    LocalErrorCase("routine", RoutineError),
    LocalErrorCase("persistence", PersistenceError),
    LocalErrorCase("capture-manifest", CaptureError),
    LocalErrorCase("journal-unsafe", JournalUnsafeError),
    LocalErrorCase("journal-conflict", JournalConflictError),
    LocalErrorCase("restore-manifest", RestoreManifestError),
    LocalErrorCase("restore-pending", RestorePendingError),
    LocalErrorCase("restore-input", RestoreInputError),
    LocalErrorCase("native-restore", NativePlaneUnavailable),
    LocalErrorCase("swarm", SwarmError),
    LocalErrorCase("swarm-worker", WorkerProtocolError),
    LocalErrorCase("update", BotUpdateError),
)
PRIVATE_VALUES = (
    ABSOLUTE_PATH,
    HOME_SEGMENT,
    CONTROLLER_URL,
    BOT_ID,
    MANIFEST_ID,
    JOURNAL_ID,
)
CASE_IDS = tuple(case.name for case in LOCAL_ERROR_CASES)


class _Console(Protocol):
    def write(self, line: str) -> None: ...


class _Manager:
    pass


def _assert_sanitized(result: str, tool_name: str) -> None:
    assert result == f"{tool_name} failed: local environment error."
    assert all(value not in result for value in PRIVATE_VALUES)


@pytest.mark.anyio
async def test_restore_local_error_is_sanitized_at_mcp_tool_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    def fail_restore(
        _gateway: _Manager,
        _options: RestoreCommandOptions,
        _console: _Console,
    ) -> None:
        raise RestoreManifestError(PRIVATE_DETAIL)

    monkeypatch.setattr(operations, "GatewayManager", _Manager)
    monkeypatch.setattr(operations, "run_gateway_restore", fail_restore)

    # When
    result = await m.server.call_tool(
        "grok_env_restore",
        {"confirmed": True},
    )

    # Then
    assert isinstance(result, CallToolResult)
    rendered = result.model_dump_json()
    assert "grok_env_restore failed: local environment error." in rendered
    assert all(value not in rendered for value in PRIVATE_VALUES)


@pytest.mark.parametrize("case", LOCAL_ERROR_CASES, ids=CASE_IDS)
def test_sync_local_error_family_is_sanitized(case: LocalErrorCase) -> None:
    # Given
    @translate_tool_errors
    def local_sync_tool() -> str:
        raise case.factory(PRIVATE_DETAIL)

    # When
    result = local_sync_tool()

    # Then
    _assert_sanitized(result, "local_sync_tool")


@pytest.mark.anyio
@pytest.mark.parametrize("case", LOCAL_ERROR_CASES, ids=CASE_IDS)
async def test_async_local_error_family_is_sanitized(case: LocalErrorCase) -> None:
    # Given
    @translate_async_tool_errors
    async def local_async_tool() -> str:
        raise case.factory(PRIVATE_DETAIL)

    # When
    result = await local_async_tool()

    # Then
    _assert_sanitized(result, "local_async_tool")
