from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import final

import pytest

from groken.env_collectors import Inventory
from groken.env_restore import (
    ReportClass,
    RestoreContext,
    RestoreOptions,
    RestoreRequest,
    RestoreRunRequest,
    RestoreRunResult,
    RoutineRestore,
    execute_restore,
    plan_restore,
)
from groken.env_restore_store import JournalState, JournalStore

MANIFEST_ID = "sha256:" + "b" * 64


def inventory(
    *,
    brew: tuple[tuple[str, str], ...] = (),
    mas: tuple[tuple[str, str, str], ...] = (),
    python: tuple[str, ...] = (),
    npm: tuple[tuple[str, str], ...] = (),
    pipx: tuple[tuple[str, str], ...] = (),
    apps: tuple[tuple[str, str, str, str], ...] = (),
) -> Inventory:
    brewfile = "".join(f'{kind} "{name}"\n' for kind, name in brew)
    python_rows = (
        ()
        if not python
        else (
            {
                "scope": "system",
                "executable": "/usr/bin/python3",
                "version": "Python 3.13",
                "requirements": list(python),
            },
        )
    )
    return Inventory(
        brewfile,
        python_rows,
        {
            "node_version": "v24",
            "prefix": "/usr/local",
            "packages": [{"name": name, "version": version} for name, version in npm],
        },
        tuple({"name": name, "version": version} for name, version in pipx),
        tuple(
            {"id": app_id, "name": name, "version": version}
            for app_id, name, version in mas
        ),
        tuple(
            {"name": name, "path": path, "bundle_id": bundle, "version": version}
            for name, path, bundle, version in apps
        ),
    )


@final
class ScriptedRunner:
    def __init__(
        self, action: Callable[[RestoreRunRequest, int], RestoreRunResult]
    ) -> None:
        self.action: Callable[[RestoreRunRequest, int], RestoreRunResult] = action
        self.requests: list[RestoreRunRequest] = []

    def run_restore(self, request: RestoreRunRequest) -> RestoreRunResult:
        self.requests.append(request)
        return self.action(request, len(self.requests))


def result(
    request: RestoreRunRequest,
    code: int = 0,
    stderr: bytes = b"",
) -> RestoreRunResult:
    return RestoreRunResult(request.argv, code, b"", stderr, False, False, None)


def restore_request(
    root: Path,
    expected: Inventory,
    current: Inventory,
    routines: tuple[RoutineRestore, ...] = (),
) -> RestoreRequest:
    brewfile: Path | None = None
    if expected.brewfile:
        brewfile = root / "artifacts" / "brew.raw"
        brewfile.parent.mkdir(parents=True, exist_ok=True)
        _ = brewfile.write_text(expected.brewfile)
    return RestoreRequest(expected, current, brewfile, routines)


def test_plan_is_grouped_exact_order_and_preserves_prompt_names_as_argv_data(
    tmp_path: Path,
) -> None:
    # Given
    expected = inventory(
        brew=(("brew", "jq"),),
        mas=(("497799835", "Xcode", "16.4"),),
        python=("httpx==0.28.1",),
        npm=(("typescript", "5.9.2"),),
        pipx=(("ruff", "0.12.9"),),
        apps=(("Demo", "/Applications/Demo.app", "com.demo", "1.2"),),
    )

    # When
    plan = plan_restore(
        restore_request(
            tmp_path,
            expected,
            inventory(),
            (RoutineRestore("repair-demo", ("/safe/repair", "--restore")),),
        )
    )

    # Then
    assert [(op.phase, op.provider) for op in plan.operations] == [
        ("restore", "brew"),
        ("restore", "mas"),
        ("restore", "python"),
        ("restore", "npm"),
        ("restore", "pipx"),
        ("restore", "application"),
        ("restore", "routine"),
    ]
    assert plan.operations[0].argv == (
        "/usr/bin/env",
        "brew",
        "bundle",
        "--file",
        str(tmp_path / "artifacts" / "brew.raw"),
    )
    assert plan.operations[0].key == "restore/brew/bundle/brewfile"
    positions = [
        plan.summary.index(f"[{group}]")
        for group in ("brew", "mas", "python", "npm", "pipx", "applications")
    ]
    assert positions == sorted(positions)


def test_interruption_resume_skips_verified_success_and_retries_running(
    tmp_path: Path,
) -> None:
    # Given
    expected = inventory(brew=(("brew", "jq"),), npm=(("typescript", "5.9.2"),))
    plan = plan_restore(restore_request(tmp_path, expected, inventory()))
    installed: set[str] = set()
    interrupted = True

    def action(request: RestoreRunRequest, _attempt: int) -> RestoreRunResult:
        nonlocal interrupted
        if request.argv[:3] == ("/usr/bin/env", "brew", "bundle"):
            installed.add("brew")
            return result(request)
        if interrupted:
            interrupted = False
            raise KeyboardInterrupt
        installed.add("npm")
        return result(request)

    def recapture() -> Inventory:
        return inventory(
            brew=(("brew", "jq"),) if "brew" in installed else (),
            npm=(("typescript", "5.9.2"),) if "npm" in installed else (),
        )

    runner = ScriptedRunner(action)
    store = JournalStore(tmp_path / "env", "bot-1", MANIFEST_ID)

    # When
    with pytest.raises(KeyboardInterrupt):
        _ = execute_restore(
            plan, RestoreContext(store, runner, recapture, RestoreOptions())
        )
    first = store.load()
    report = execute_restore(
        plan, RestoreContext(store, runner, recapture, RestoreOptions())
    )

    # Then
    assert first is not None
    assert [row.state for row in first.operations] == [
        JournalState.SUCCEEDED,
        JournalState.RUNNING,
    ]
    assert [request.argv for request in runner.requests].count(
        plan.operations[0].argv
    ) == 1
    assert [request.argv for request in runner.requests].count(
        (
            "/usr/bin/env",
            "npm",
            "install",
            "--global",
            "typescript@5.9.2",
        )
    ) == 2
    assert report.count(ReportClass.RESTORED) == 2
    assert report.exit_code == 0


def test_runner_argv_mismatch_never_claims_success(tmp_path: Path) -> None:
    # Given
    expected = inventory(brew=(("brew", "jq"),))
    plan = plan_restore(restore_request(tmp_path, expected, inventory()))

    def misleading(request: RestoreRunRequest, _attempt: int) -> RestoreRunResult:
        del request
        return RestoreRunResult(
            ("brew", "uninstall", "jq"),
            0,
            b"ok",
            b"",
            False,
            False,
            None,
        )

    captures = iter((inventory(), expected))
    store = JournalStore(tmp_path / "env", "bot-1", MANIFEST_ID)

    # When
    report = execute_restore(
        plan,
        RestoreContext(
            store, ScriptedRunner(misleading), lambda: next(captures), RestoreOptions()
        ),
    )

    # Then
    journal = store.load()
    assert journal is not None and journal.operations[0].state is JournalState.FAILED
    assert journal.operations[0].error == "runner returned a different argv"
    assert report.exit_code == 1


def test_manual_requires_retry_manual_and_is_bounded(tmp_path: Path) -> None:
    # Given
    expected = inventory(mas=(("497799835", "Xcode", "16.4"),))
    plan = plan_restore(restore_request(tmp_path, expected, inventory()))

    def signed_out(request: RestoreRunRequest, _attempt: int) -> RestoreRunResult:
        return result(request, 1, b"Not signed in with an Apple ID")

    runner = ScriptedRunner(signed_out)
    store = JournalStore(tmp_path / "env", "bot-1", MANIFEST_ID)

    # When
    first = execute_restore(
        plan, RestoreContext(store, runner, inventory, RestoreOptions())
    )
    second = execute_restore(
        plan, RestoreContext(store, runner, inventory, RestoreOptions())
    )
    third = execute_restore(
        plan,
        RestoreContext(store, runner, inventory, RestoreOptions(retry_manual=True)),
    )

    # Then
    assert len(runner.requests) == 2
    assert all(
        report.count(ReportClass.MANUAL_ACTION) == 1
        for report in (first, second, third)
    )
    journal = store.load()
    assert journal is not None and journal.operations[0].attempts == 2
    assert journal.operations[0].state is JournalState.MANUAL


def test_post_recap_reports_restored_drift_missing_extra_and_manual(
    tmp_path: Path,
) -> None:
    # Given
    expected = inventory(
        brew=(("brew", "jq"), ("brew", "wget")),
        npm=(("typescript", "5.9.2"),),
        apps=(("Demo", "/Applications/Demo.app", "com.demo", "1.2"),),
    )
    plan = plan_restore(restore_request(tmp_path, expected, inventory()))
    post = inventory(
        brew=(("brew", "jq"), ("brew", "extra")),
        npm=(("typescript", "6.0.0"),),
        apps=(),
    )
    runner = ScriptedRunner(lambda request, _attempt: result(request))

    # When
    report = execute_restore(
        plan,
        RestoreContext(
            JournalStore(tmp_path / "env", "bot-1", MANIFEST_ID),
            runner,
            lambda: post,
            RestoreOptions(),
        ),
    )

    # Then
    assert {kind: report.count(kind) for kind in ReportClass} == {
        ReportClass.RESTORED: 1,
        ReportClass.VERSION_DRIFT: 1,
        ReportClass.MISSING: 1,
        ReportClass.EXTRA: 1,
        ReportClass.MANUAL_ACTION: 1,
    }
    assert report.exit_code == 1
    journal = JournalStore(tmp_path / "env", "bot-1", MANIFEST_ID).load()
    assert journal is not None
    assert (
        next(row for row in journal.operations if row.item == "Brewfile").state
        is JournalState.FAILED
    )
