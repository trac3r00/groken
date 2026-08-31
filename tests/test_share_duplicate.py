from __future__ import annotations

import fcntl
import multiprocessing
import os
import subprocess
import sys
from multiprocessing.synchronize import Barrier
from pathlib import Path

import pytest

from groken.share_store import DuplicateShareError, ShareRecord, ShareStore


def _create_duplicate_after_barrier(
    path: str,
    result_path: str,
    barrier: Barrier,
) -> None:
    _ = barrier.wait()
    try:
        _ = ShareStore(Path(path)).create("shared", "bot-id", "Bot Name")
    except DuplicateShareError:
        result = "duplicate"
    else:
        result = "created"
    _ = Path(result_path).write_text(result)


def test_duplicate_create_raises_typed_error_and_preserves_transaction(
    tmp_path: Path,
) -> None:
    # Given
    path = tmp_path / "shares.json"
    store = ShareStore(path)
    original = store.create("shared", "bot-id", "Bot Name")
    persisted_before_duplicate = path.read_bytes()

    # When
    with pytest.raises(DuplicateShareError) as raised:
        _ = store.create("shared", "other-id", "Other Bot")

    # Then
    assert raised.value.name == "shared"
    assert path.read_bytes() == persisted_before_duplicate
    assert store.list() == [ShareRecord("shared", "bot-id", "Bot Name")]
    assert store.authenticate(original.token) == original.record
    descriptor = os.open(f"{path}.lock", os.O_RDWR)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(descriptor)


def test_nonduplicate_create_works_after_duplicate_failure(tmp_path: Path) -> None:
    # Given
    store = ShareStore(tmp_path / "shares.json")
    _ = store.create("shared", "bot-id", "Bot Name")
    with pytest.raises(DuplicateShareError):
        _ = store.create("shared", "other-id", "Other Bot")

    # When
    created = store.create("second", "second-id", "Second Bot")

    # Then
    assert created.record == ShareRecord("second", "second-id", "Second Bot")
    assert {record.name for record in store.list()} == {"shared", "second"}


def test_concurrent_duplicate_create_has_one_typed_loser(tmp_path: Path) -> None:
    # Given
    path = tmp_path / "shares.json"
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(2)
    result_paths = [tmp_path / f"result-{index}" for index in range(2)]
    processes = [
        context.Process(
            target=_create_duplicate_after_barrier,
            args=(str(path), str(result_path), barrier),
        )
        for result_path in result_paths
    ]

    # When
    for process in processes:
        process.start()
    try:
        for process in processes:
            process.join(timeout=10)
        assert all(not process.is_alive() for process in processes)
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
            process.join()

    # Then
    assert all(process.exitcode == 0 for process in processes)
    assert sorted(result_path.read_text() for result_path in result_paths) == [
        "created",
        "duplicate",
    ]
    assert ShareStore(path).list() == [ShareRecord("shared", "bot-id", "Bot Name")]


def test_duplicate_cli_create_exits_cleanly_without_secret_or_path_leak(
    tmp_path: Path,
) -> None:
    # Given
    home = tmp_path / "home"
    shares_path = home / ".config" / "groken" / "shares.json"
    original = ShareStore(shares_path).create("shared", "bot-id", "Bot Name")
    environment = os.environ.copy()
    environment["HOME"] = str(home)
    command = """
from groken import cli

class FakeRoster:
    def command(self, method: str, args: dict[str, object] | None = None) -> object:
        assert method == "listAgents"
        assert args is None
        return [{"id": "bot-id", "name": "Bot Name"}]

cli.cmd_share_create("shared", "Bot Name", manager=FakeRoster())
"""

    # When
    result = subprocess.run(
        [sys.executable, "-c", command],
        capture_output=True,
        check=False,
        env=environment,
        text=True,
        timeout=10,
    )

    # Then
    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == "share name already exists: shared\n"
    assert "Traceback" not in result.stderr
    assert original.token not in result.stderr
    assert str(shares_path) not in result.stderr
