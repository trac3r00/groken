from __future__ import annotations

from pathlib import Path
from typing import final

import pytest

from groken.env_collectors import (
    Inventory,
    NativePlaneUnavailable,
)
from groken.env_restore import (
    RestoreContext,
    RestoreOptions,
    RestoreRequest,
    RestoreRunRequest,
    RestoreRunResult,
    execute_restore,
    plan_restore,
)
from groken.env_restore_store import JournalState, JournalStore

MANIFEST_ID = "sha256:" + "f" * 64


def inventory(*, brew: bool = False, npm: bool = False) -> Inventory:
    return Inventory(
        'brew "jq"\n' if brew else "",
        (),
        {
            "node_version": "v24",
            "prefix": "/usr/local",
            "packages": ([{"name": "typescript", "version": "5.9.2"}] if npm else []),
        },
        (),
        (),
        (),
    )


def restore_request(root: Path, expected: Inventory) -> RestoreRequest:
    brewfile = root / "artifacts" / "brew.raw" if expected.brewfile else None
    if brewfile is not None:
        brewfile.parent.mkdir(parents=True)
        _ = brewfile.write_text(expected.brewfile)
    return RestoreRequest(expected, inventory(), brewfile, ())


@final
class LossThenSuccessRunner:
    def __init__(self) -> None:
        self.available = False
        self.brew = False
        self.npm = False
        self.requests: list[RestoreRunRequest] = []

    def run_restore(self, request: RestoreRunRequest) -> RestoreRunResult:
        self.requests.append(request)
        if not self.available:
            raise NativePlaneUnavailable("native runner disconnected")
        if request.argv[:3] == ("/usr/bin/env", "brew", "bundle"):
            self.brew = True
        if request.argv[:2] == ("/usr/bin/env", "npm"):
            self.npm = True
        return RestoreRunResult(request.argv, 0, b"ok", b"", False, False, None)


def test_runner_loss_persists_failure_and_safe_resume(tmp_path: Path) -> None:
    # Given
    expected = inventory(brew=True, npm=True)
    plan = plan_restore(restore_request(tmp_path, expected))
    runner = LossThenSuccessRunner()
    store = JournalStore(tmp_path / "env", "bot-1", MANIFEST_ID)

    def capture() -> Inventory:
        return inventory(brew=runner.brew, npm=runner.npm)

    # When
    failed = execute_restore(
        plan, RestoreContext(store, runner, capture, RestoreOptions())
    )
    runner.available = True
    resumed = execute_restore(
        plan, RestoreContext(store, runner, capture, RestoreOptions())
    )

    # Then
    assert failed.exit_code == 1
    assert resumed.exit_code == 0
    assert [request.argv for request in runner.requests] == [
        plan.operations[0].argv,
        plan.operations[0].argv,
        ("/usr/bin/env", "npm", "install", "--global", "typescript@5.9.2"),
    ]
    journal = store.load()
    assert journal is not None
    assert all(row.state is JournalState.SUCCEEDED for row in journal.operations)


def test_timed_out_runner_is_failed_without_waiting_or_retry_loop(
    tmp_path: Path,
) -> None:
    # Given
    expected = inventory(brew=True)
    plan = plan_restore(restore_request(tmp_path, expected))
    requests: list[RestoreRunRequest] = []

    @final
    class TimedOutRunner:
        def run_restore(self, request: RestoreRunRequest) -> RestoreRunResult:
            requests.append(request)
            return RestoreRunResult(
                request.argv,
                None,
                b"partial",
                b"deadline",
                True,
                False,
                None,
            )

    store = JournalStore(tmp_path / "env", "bot-1", MANIFEST_ID)

    # When
    report = execute_restore(
        plan,
        RestoreContext(store, TimedOutRunner(), inventory, RestoreOptions()),
    )

    # Then
    assert report.exit_code == 1
    assert len(requests) == 1 and requests[0].timeout_ms == 30_000
    journal = store.load()
    assert journal is not None
    assert journal.operations[0].state is JournalState.FAILED
    assert journal.operations[0].error == "partialdeadline"
    assert not store.lock_path.exists()


def test_partial_post_recapture_preserves_success_for_verified_resume(
    tmp_path: Path,
) -> None:
    # Given
    expected = inventory(brew=True)
    plan = plan_restore(restore_request(tmp_path, expected))
    calls = 0

    @final
    class Runner:
        def run_restore(self, request: RestoreRunRequest) -> RestoreRunResult:
            nonlocal calls
            calls += 1
            return RestoreRunResult(
                request.argv,
                0,
                b"ok",
                b"",
                False,
                False,
                None,
            )

    capture_calls = 0
    store = JournalStore(tmp_path / "env", "bot-1", MANIFEST_ID)

    def interrupted_capture() -> Inventory:
        nonlocal capture_calls
        capture_calls += 1
        if capture_calls == 2:
            raise NativePlaneUnavailable("partial recapture")
        return inventory()

    # When / Then
    with pytest.raises(NativePlaneUnavailable, match="partial recapture"):
        _ = execute_restore(
            plan, RestoreContext(store, Runner(), interrupted_capture, RestoreOptions())
        )
    journal = store.load()
    assert journal is not None and journal.operations[0].state is JournalState.SUCCEEDED
    report = execute_restore(
        plan, RestoreContext(store, Runner(), lambda: expected, RestoreOptions())
    )
    assert calls == 1
    assert report.exit_code == 0
    assert not store.lock_path.exists()
