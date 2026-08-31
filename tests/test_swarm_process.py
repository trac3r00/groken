from __future__ import annotations

import os
import selectors
import signal
import subprocess
import sys
from collections.abc import Sequence

import pytest

from groken.swarm_process import (
    AskFailure,
    AskJob,
    SubprocessRoundExecutor,
    launch_worker,
)

HUNG_WORKER = "import sys,time; sys.stdin.buffer.read(); time.sleep(60)"
INTERRUPT_WORKER = (
    "import os,signal,sys,time; "
    "sys.stdin.buffer.read(); "
    "os.kill(os.getppid(), signal.SIGUSR1); "
    "time.sleep(60)"
)
PROCESS_EVENT_TIMEOUT_S = 30


class RecordingLauncher:
    def __init__(self) -> None:
        self.processes: list[subprocess.Popen[bytes]] = []

    def __call__(self, command: Sequence[str]) -> subprocess.Popen[bytes]:
        process = launch_worker(command)
        self.processes.append(process)
        return process


def test_hung_fresh_interpreter_is_killed_at_deadline_without_residue() -> None:
    # Given
    launcher = RecordingLauncher()
    executor = SubprocessRoundExecutor(
        command=(sys.executable, "-c", HUNG_WORKER),
        launcher=launcher,
    )

    # When
    results = executor.execute((AskJob("hung-id", "task"),), 0.05)

    # Then
    assert results == (AskFailure("orchestration timed out after 0.05s"),)
    assert len(launcher.processes) == 1
    assert all(process.poll() is not None for process in launcher.processes)


def test_keyboard_interrupt_kills_and_waits_for_hung_fresh_interpreter() -> None:
    # Given a fresh supervisor interpreter that reports its worker before blocking
    supervisor_code = f"""
import signal
import sys
from groken.swarm_process import AskJob, SubprocessRoundExecutor, launch_worker

signal.signal(signal.SIGUSR1, lambda _signum, _frame: print("READY", flush=True))

class ReportingLauncher:
    def __call__(self, command):
        process = launch_worker(command)
        print(f\"WORKER {{process.pid}}\", flush=True)
        return process

executor = SubprocessRoundExecutor(
    command=(sys.executable, \"-c\", {INTERRUPT_WORKER!r}),
    launcher=ReportingLauncher(),
)
try:
    executor.execute((AskJob(\"hung-id\", \"task\"),), 30)
except KeyboardInterrupt:
    print(\"INTERRUPTED\", flush=True)
"""
    supervisor = subprocess.Popen(
        (sys.executable, "-c", supervisor_code),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert supervisor.stdout is not None
    worker_pid = 0

    try:
        with selectors.DefaultSelector() as selector:
            selector.register(supervisor.stdout, selectors.EVENT_READ)
            assert selector.select(PROCESS_EVENT_TIMEOUT_S), "subprocess worker did not start"
            started = supervisor.stdout.readline().strip()
            assert selector.select(PROCESS_EVENT_TIMEOUT_S), "subprocess worker did not consume its request"
            ready = supervisor.stdout.readline().strip()
        worker_pid = int(started.removeprefix("WORKER "))
        assert ready == "READY"

        # When
        os.kill(supervisor.pid, signal.SIGINT)
        stdout, stderr = supervisor.communicate(timeout=PROCESS_EVENT_TIMEOUT_S)

        # Then
        assert supervisor.returncode == 0, stderr
        assert "INTERRUPTED" in stdout
        with pytest.raises(ProcessLookupError):
            os.kill(worker_pid, 0)
    finally:
        if supervisor.poll() is None:
            supervisor.terminate()
            try:
                supervisor.wait(1)
            except subprocess.TimeoutExpired:
                supervisor.kill()
                supervisor.wait()
        if worker_pid:
            try:
                os.kill(worker_pid, signal.SIGKILL)
            except ProcessLookupError:
                worker_pid = 0
