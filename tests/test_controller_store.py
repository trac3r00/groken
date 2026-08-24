from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import final

import pytest

from groken.controller_models import (
    CompletionRequest,
    ControllerJobRequest,
    ControllerJobStatus,
)
from groken.controller_store import ControllerStore


@final
class Clock:
    def __init__(self) -> None:
        self.now: datetime = datetime(2026, 8, 21, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


def request() -> ControllerJobRequest:
    return ControllerJobRequest(task="verify", workspace="qa")


def test_lease_recovers_for_worker_and_reassigns_after_expiry(tmp_path: Path) -> None:
    clock = Clock()
    store = ControllerStore(tmp_path, now=clock, lease_seconds=60)
    _ = store.create("job-1", request())

    first = store.lease_next("worker-a")
    assert first is not None
    recovered = ControllerStore(tmp_path, now=clock, lease_seconds=60).lease_next("worker-a")
    assert recovered is not None
    assert recovered.lease_id == first.lease_id

    clock.now += timedelta(seconds=61)
    reassigned = ControllerStore(tmp_path, now=clock, lease_seconds=60).lease_next("worker-b")
    assert reassigned is not None
    assert reassigned.job_id == first.job_id
    assert reassigned.lease_id != first.lease_id

    stale = CompletionRequest(
        worker_id="worker-a",
        job_id="job-1",
        lease_id=first.lease_id,
        status=ControllerJobStatus.COMPLETED,
        result="stale",
    )
    with pytest.raises(ValueError, match="lease"):
        _ = store.complete(stale)


def test_legacy_lease_without_metadata_is_requeued(tmp_path: Path) -> None:
    store = ControllerStore(tmp_path)
    record = store.create("job-1", request())
    record.status = ControllerJobStatus.LEASED
    record.leased_by = "old-worker"
    store.write(record)

    lease = ControllerStore(tmp_path).lease_next("new-worker")

    assert lease is not None
    assert lease.job_id == "job-1"
    assert lease.lease_id


def test_identical_completion_retry_is_idempotent(tmp_path: Path) -> None:
    store = ControllerStore(tmp_path)
    _ = store.create("job-1", request())
    lease = store.lease_next("worker-a")
    assert lease is not None
    completion = CompletionRequest(
        worker_id="worker-a",
        job_id="job-1",
        lease_id=lease.lease_id,
        status=ControllerJobStatus.COMPLETED,
        result="done",
    )

    first = store.complete(completion)
    retried = ControllerStore(tmp_path).complete(completion)

    assert first.status is ControllerJobStatus.COMPLETED
    assert retried == first
    conflicting = completion.model_copy(update={"result": "different"})
    with pytest.raises(ValueError, match="conflicting"):
        _ = store.complete(conflicting)
