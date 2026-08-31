"""Fresh-interpreter and direct adapters for concurrent swarm rounds."""

from __future__ import annotations

import subprocess
import sys
import time
from collections.abc import Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import Final, Protocol, assert_never

from .swarm_worker import (
    WorkerAnswer,
    WorkerFailure,
    WorkerProtocolError,
    WorkerRequest,
    decode_result,
    encode_request,
)

_JOIN_TIMEOUT_S: Final = 1.0
DEFAULT_WORKER_COMMAND: Final = (sys.executable, "-m", "groken.swarm_worker")


class AskManager(Protocol):
    def ask(self, agent_id: str, text: str, timeout_s: float = 600) -> str: ...


@dataclass(frozen=True, slots=True)
class AskJob:
    agent_id: str
    prompt: str


@dataclass(frozen=True, slots=True)
class AskAnswer:
    text: str


@dataclass(frozen=True, slots=True)
class AskFailure:
    error: str


AskResult = AskAnswer | AskFailure


class RoundExecutor(Protocol):
    def execute(
        self, jobs: tuple[AskJob, ...], timeout_s: float
    ) -> tuple[AskResult, ...]: ...


class ProcessLauncher(Protocol):
    def __call__(self, command: Sequence[str]) -> subprocess.Popen[bytes]: ...


def _failure_detail(exc: BaseException) -> str:
    return str(exc) or type(exc).__name__


def _direct_result(future: Future[str]) -> AskResult:
    match future.exception():
        case None:
            return AskAnswer(future.result())
        case BaseException() as exc:
            return AskFailure(_failure_detail(exc))
        case _ as unreachable:
            assert_never(unreachable)


@dataclass(frozen=True, slots=True)
class DirectRoundExecutor:
    """Concurrent in-process adapter for deterministic manager fakes."""

    manager: AskManager

    def execute(
        self, jobs: tuple[AskJob, ...], timeout_s: float
    ) -> tuple[AskResult, ...]:
        with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
            futures = tuple(
                pool.submit(
                    self.manager.ask,
                    job.agent_id,
                    job.prompt,
                    timeout_s,
                )
                for job in jobs
            )
            return tuple(_direct_result(future) for future in futures)


def launch_worker(command: Sequence[str]) -> subprocess.Popen[bytes]:
    """Start one worker with owned binary protocol pipes and no shell."""
    return subprocess.Popen(
        tuple(command),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _worker_result(stdout: bytes) -> AskResult:
    try:
        result = decode_result(stdout)
    except WorkerProtocolError:
        return AskFailure("worker returned an invalid result")
    match result:
        case WorkerAnswer(text=text):
            return AskAnswer(text)
        case WorkerFailure(error=error):
            return AskFailure(error)
        case _ as unreachable:
            assert_never(unreachable)


def _stop_workers(processes: Sequence[subprocess.Popen[bytes]]) -> None:
    for process in processes:
        if process.poll() is None:
            process.terminate()
    deadline = time.monotonic() + _JOIN_TIMEOUT_S
    for process in processes:
        if process.poll() is not None:
            continue
        try:
            process.wait(max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            continue
    for process in processes:
        if process.poll() is None:
            process.kill()
    for process in processes:
        process.wait()


def _close_pipes(processes: Sequence[subprocess.Popen[bytes]]) -> None:
    for process in processes:
        for pipe in (process.stdin, process.stdout, process.stderr):
            if pipe is not None and not pipe.closed:
                pipe.close()


@dataclass(frozen=True, slots=True)
class SubprocessRoundExecutor:
    """Execute each ask in a killable fresh interpreter."""

    command: Sequence[str] = DEFAULT_WORKER_COMMAND
    launcher: ProcessLauncher = launch_worker

    def execute(
        self, jobs: tuple[AskJob, ...], timeout_s: float
    ) -> tuple[AskResult, ...]:
        deadline = time.monotonic() + timeout_s
        processes: list[subprocess.Popen[bytes]] = []
        pool = ThreadPoolExecutor(max_workers=len(jobs))
        futures: dict[Future[tuple[bytes, bytes]], int] = {}
        results: list[AskResult | None] = [None] * len(jobs)
        try:
            for index, job in enumerate(jobs):
                process = self.launcher(self.command)
                processes.append(process)
                request = encode_request(
                    WorkerRequest(job.agent_id, job.prompt, timeout_s)
                )
                futures[pool.submit(process.communicate, request)] = index
            pending = set(futures)
            while pending:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                completed, pending = wait(
                    pending,
                    timeout=remaining,
                    return_when=FIRST_COMPLETED,
                )
                for future in completed:
                    index = futures[future]
                    match future.exception():
                        case None:
                            stdout, _stderr = future.result()
                            results[index] = _worker_result(stdout)
                        case BaseException() as exc:
                            results[index] = AskFailure(_failure_detail(exc))
                        case _ as unreachable:
                            assert_never(unreachable)
            for future in pending:
                results[futures[future]] = AskFailure(
                    f"orchestration timed out after {timeout_s:g}s"
                )
            return tuple(
                result
                if result is not None
                else AskFailure("worker result unavailable")
                for result in results
            )
        finally:
            _stop_workers(processes)
            pool.shutdown(wait=True, cancel_futures=True)
            _close_pipes(processes)
