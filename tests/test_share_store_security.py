import multiprocessing
import os
from multiprocessing.synchronize import Barrier
from pathlib import Path

import pytest

from groken.share_store import (
    BlankShareFieldError,
    ShareStore,
    ShareStoreDataError,
)


def _create_after_barrier(path: str, name: str, barrier: Barrier) -> None:
    barrier.wait()
    ShareStore(Path(path)).create(name, f"id-{name}", f"bot-{name}")


def _create_and_revoke_after_barrier(
    path: str,
    name: str,
    barrier: Barrier,
    action: str,
) -> None:
    barrier.wait()
    store = ShareStore(Path(path))
    if action == "create":
        store.create(name, "new-id", "new-bot")
    else:
        store.revoke("original")


def test_create_persists_immutable_bot_identity_and_only_hash(tmp_path: Path) -> None:
    path = tmp_path / "shares.json"
    grant = ShareStore(path).create("alice", "bot-id", "Bot Name")

    assert grant.record.name == "alice"
    assert grant.record.bot_id == "bot-id"
    assert grant.record.bot_name == "Bot Name"
    assert grant.record.revoked is False
    assert grant.token not in path.read_text()
    assert os.stat(path).st_mode & 0o777 == 0o600
    assert os.stat(path.with_name("shares.json.lock")).st_mode & 0o777 == 0o600


def test_blank_identity_fields_raise_typed_error(tmp_path: Path) -> None:
    store = ShareStore(tmp_path / "shares.json")

    with pytest.raises(BlankShareFieldError):
        store.create(" ", "bot-id", "Bot Name")
    with pytest.raises(BlankShareFieldError):
        store.create("alice", "", "Bot Name")
    with pytest.raises(BlankShareFieldError):
        store.create("alice", "bot-id", "\t")


@pytest.mark.parametrize("payload", ["{malformed", '{"name":"not-a-list"}'])
def test_malformed_store_authentication_fails_closed(
    tmp_path: Path, payload: str
) -> None:
    # Given
    path = tmp_path / "shares.json"
    path.write_text(payload)

    # When
    result = ShareStore(path).authenticate("any-token")

    # Then
    assert result is None


@pytest.mark.parametrize("payload", ["{malformed", '{"name":"not-a-list"}'])
@pytest.mark.parametrize("operation", ["create", "revoke"])
def test_malformed_store_mutation_raises_actionable_error_without_rewriting(
    tmp_path: Path, payload: str, operation: str
) -> None:
    # Given
    path = tmp_path / "shares.json"
    path.write_text(payload)
    persisted_before_mutation = path.read_bytes()
    store = ShareStore(path)

    # When
    with pytest.raises(ShareStoreDataError) as raised:
        if operation == "create":
            _ = store.create("alice", "bot-id", "Bot Name")
        else:
            _ = store.revoke("alice")

    # Then
    assert "repair or remove" in str(raised.value)
    assert path.read_bytes() == persisted_before_mutation


def test_malformed_rows_cannot_authenticate(tmp_path: Path) -> None:
    path = tmp_path / "shares.json"
    path.write_text(
        '[{"name":"bad","bot_id":"id","bot_name":"name",'
        '"token_hash":"not-a-sha256","revoked":false}]'
    )

    assert ShareStore(path).authenticate("not-a-sha256") is None
    assert ShareStore(path).list() == []


def test_simultaneous_creates_do_not_lose_records(tmp_path: Path) -> None:
    path = tmp_path / "shares.json"
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(2)
    processes = [
        context.Process(
            target=_create_after_barrier,
            args=(str(path), f"share-{index}", barrier),
        )
        for index in range(2)
    ]

    for process in processes:
        process.start()
    for process in processes:
        process.join()

    assert all(process.exitcode == 0 for process in processes)
    assert {record.name for record in ShareStore(path).list()} == {
        "share-0",
        "share-1",
    }


def test_concurrent_create_cannot_resurrect_revocation(tmp_path: Path) -> None:
    path = tmp_path / "shares.json"
    store = ShareStore(path)
    original = store.create("original", "original-id", "Original Bot")
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(2)
    processes = [
        context.Process(
            target=_create_and_revoke_after_barrier,
            args=(str(path), "new", barrier, action),
        )
        for action in ("create", "revoke")
    ]

    for process in processes:
        process.start()
    for process in processes:
        process.join()

    assert all(process.exitcode == 0 for process in processes)
    records = {record.name: record for record in store.list()}
    assert records["original"].revoked is True
    assert records["new"].bot_id == "new-id"
    assert store.authenticate(original.token) is None
