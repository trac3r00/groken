from __future__ import annotations

import json
import os
import socket
import stat
from collections.abc import Callable
from pathlib import Path
from typing import TypeGuard

import pytest

from groken.env_restore_store import (
    JournalConflictError,
    JournalEntry,
    JournalState,
    JournalStore,
    JournalUnsafeError,
    JsonValue,
)

MANIFEST_ID = "sha256:" + "a" * 64


def decode(raw: str, loader: Callable[[str], JsonValue] = json.loads) -> JsonValue:
    return loader(raw)


def record(value: JsonValue) -> TypeGuard[dict[str, JsonValue]]:
    return isinstance(value, dict)


def entry(
    *, argv: tuple[str, ...] = ("/usr/bin/env", "brew", "bundle")
) -> JournalEntry:
    return JournalEntry(
        key="restore/brew/bundle/brewfile",
        item="Brewfile",
        argv=argv,
        state=JournalState.PENDING,
        attempts=0,
        idempotency_key=None,
        started_at=None,
        ended_at=None,
        exit_code=None,
        signal=None,
        truncated=False,
        error=None,
    )


def test_journal_round_trip_is_atomic_private_and_schema_exact(tmp_path: Path) -> None:
    # Given
    store = JournalStore(tmp_path / "env", "bot-1", MANIFEST_ID)

    # When
    journal = store.ensure((entry(),))

    # Then
    payload = decode(store.path.read_text())
    assert record(payload)
    operations = payload["operations"]
    assert record(operations)
    operation = operations[entry().key]
    assert record(operation)
    assert set(payload) == {"schema_version", "bot_id", "manifest_id", "operations"}
    assert set(operation) == {
        "item",
        "argv",
        "state",
        "attempts",
        "idempotency_key",
        "started_at",
        "ended_at",
        "exit_code",
        "signal",
        "truncated",
        "error",
    }
    assert journal.operations == (entry(),)
    assert stat.S_IMODE(store.path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert list(store.path.parent.glob(".*.tmp")) == []


def test_journal_rejects_key_body_conflict_and_foreign_identity(tmp_path: Path) -> None:
    # Given
    store = JournalStore(tmp_path / "env", "bot-1", MANIFEST_ID)
    _ = store.ensure((entry(),))

    # When / Then
    with pytest.raises(JournalConflictError, match="different operation"):
        _ = store.ensure((entry(argv=("brew", "uninstall", "jq")),))
    payload = decode(store.path.read_text())
    assert record(payload)
    payload["bot_id"] = "bot-2"
    _ = store.path.write_text(json.dumps(payload))
    with pytest.raises(JournalUnsafeError, match="identity"):
        _ = store.load()


@pytest.mark.parametrize(
    "mutation", ["journal-symlink", "journal-directory", "bot-symlink", "malformed"]
)
def test_journal_rejects_unsafe_or_malformed_paths(
    tmp_path: Path, mutation: str
) -> None:
    # Given
    root = tmp_path / "env"
    store = JournalStore(root, "bot-1", MANIFEST_ID)
    outside = tmp_path / "outside"
    if mutation == "bot-symlink":
        outside.mkdir()
        (root).mkdir()
        (root / "bot-1").symlink_to(outside, target_is_directory=True)
    else:
        store.path.parent.mkdir(parents=True)
        if mutation == "journal-symlink":
            _ = outside.write_text("{}")
            store.path.symlink_to(outside)
        elif mutation == "journal-directory":
            store.path.mkdir()
        else:
            _ = store.path.write_text('{"schema_version":1}')

    # When / Then
    with pytest.raises(JournalUnsafeError, match="regular|safe|schema|directory"):
        _ = store.load()


def test_journal_rejects_permissive_mode_and_invalid_timestamp(tmp_path: Path) -> None:
    # Given
    store = JournalStore(tmp_path / "env", "bot-1", MANIFEST_ID)
    _ = store.ensure((entry(),))
    store.path.chmod(0o644)

    # When / Then
    with pytest.raises(JournalUnsafeError, match="0600"):
        _ = store.load()
    store.path.chmod(0o600)
    payload = decode(store.path.read_text())
    assert record(payload)
    operations = payload["operations"]
    assert record(operations)
    operation = operations[entry().key]
    assert record(operation)
    operation["started_at"] = "not-a-timestamp"
    _ = store.path.write_text(json.dumps(payload))
    with pytest.raises(JournalUnsafeError, match="timestamp"):
        _ = store.load()


@pytest.mark.parametrize(
    ("key", "item"),
    [
        ("restore/python/..%2Fescape/demo", "demo"),
        ("restore/npm/global/--registry", "--registry"),
    ],
)
def test_journal_rejects_unsafe_provider_identity(
    tmp_path: Path, key: str, item: str
) -> None:
    # Given
    store = JournalStore(tmp_path / "env", "bot-1", MANIFEST_ID)
    _ = store.ensure((entry(),))
    payload = decode(store.path.read_text())
    assert record(payload)
    operations = payload["operations"]
    assert record(operations)
    operation = operations.pop(entry().key)
    assert record(operation)
    operation["item"] = item
    operations[key] = operation
    _ = store.path.write_text(json.dumps(payload))

    # When / Then
    with pytest.raises(JournalUnsafeError, match="scope|item|unsafe|invalid"):
        _ = store.load()


def test_same_manifest_lock_records_owner_blocks_live_owner_and_cleans_up(
    tmp_path: Path,
) -> None:
    # Given
    store = JournalStore(tmp_path / "env", "bot-1", MANIFEST_ID)

    # When / Then
    with store.lock():
        owner = decode(store.lock_path.read_text())
        assert record(owner)
        assert set(owner) == {
            "schema_version",
            "pid",
            "hostname",
            "process_identity",
            "started_at",
        }
        assert owner["pid"] == os.getpid()
        assert owner["hostname"] == socket.gethostname()
        with (
            pytest.raises(JournalConflictError, match="already in progress"),
            store.lock(),
        ):
            pass
    assert not store.lock_path.exists()


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {
            "schema_version": 1,
            "pid": 99999999,
            "hostname": "foreign.example",
            "process_identity": "foreign",
            "started_at": "2026-08-26T00:00:00+00:00",
        },
    ],
)
def test_malformed_or_foreign_lock_is_not_recovered(
    tmp_path: Path, payload: dict[str, JsonValue]
) -> None:
    # Given
    store = JournalStore(tmp_path / "env", "bot-1", MANIFEST_ID)
    store.lock_path.parent.mkdir(parents=True)
    _ = store.lock_path.write_text(json.dumps(payload))
    store.lock_path.chmod(0o600)

    # When / Then
    with (
        pytest.raises((JournalUnsafeError, JournalConflictError), match="lock|foreign"),
        store.lock(),
    ):
        pass
