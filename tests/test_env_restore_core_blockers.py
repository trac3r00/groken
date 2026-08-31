from __future__ import annotations

import json
import socket
from collections.abc import Callable
from datetime import UTC, datetime
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
    execute_restore,
    plan_restore,
)
from groken.env_restore_store import JournalStore
from groken.env_restore_validation import RestoreInputError

MANIFEST_ID = "sha256:" + "9" * 64


def inventory(
    *,
    brewfile: str = "",
    python_scope: str | None = None,
    python_executable: str = "/usr/bin/python3",
    requirement: str = "demo==1.0",
    npm: str | None = None,
    pipx: str | None = None,
    mas: str | None = None,
    app: str | None = None,
) -> Inventory:
    python = (
        ()
        if python_scope is None
        else (
            {
                "scope": python_scope,
                "executable": python_executable,
                "version": "Python 3.13",
                "requirements": [requirement],
            },
        )
    )
    return Inventory(
        brewfile,
        python,
        {
            "node_version": "v24",
            "prefix": "/usr/local",
            "packages": ([] if npm is None else [{"name": npm, "version": "1.0"}]),
        },
        () if pipx is None else ({"name": pipx, "version": "1.0"},),
        () if mas is None else ({"id": mas, "name": "App", "version": "1.0"},),
        ()
        if app is None
        else (
            {
                "name": "App",
                "path": "/Applications/App.app",
                "bundle_id": app,
                "version": "1.0",
            },
        ),
    )


def request(tmp_path: Path, expected: Inventory, current: Inventory) -> RestoreRequest:
    brewfile_path: Path | None = None
    if expected.brewfile:
        brewfile_path = tmp_path / "trusted" / "artifacts" / "brew.raw"
        brewfile_path.parent.mkdir(parents=True)
        _ = brewfile_path.write_text(expected.brewfile)
    return RestoreRequest(expected, current, brewfile_path, ())


@final
class Runner:
    def __init__(self, action: Callable[[RestoreRunRequest], RestoreRunResult]) -> None:
        self._action = action
        self.requests: list[RestoreRunRequest] = []

    def run_restore(self, request: RestoreRunRequest) -> RestoreRunResult:
        self.requests.append(request)
        return self._action(request)


def success(command: RestoreRunRequest) -> RestoreRunResult:
    return RestoreRunResult(command.argv, 0, b"ok", b"", False, False, None)


def test_brew_uses_one_trusted_brewfile_bundle_argv(tmp_path: Path) -> None:
    # Given
    expected = inventory(brewfile='brew "jq"\ncask "rectangle"\n')
    restore_request = request(tmp_path, expected, inventory())

    # When
    plan = plan_restore(restore_request)

    # Then
    assert len([row for row in plan.operations if row.provider == "brew"]) == 1
    assert plan.operations[0].argv == (
        "/usr/bin/env",
        "brew",
        "bundle",
        "--file",
        str(restore_request.brewfile_path),
    )
    assert plan.operations[0].key == "restore/brew/bundle/brewfile"


@pytest.mark.parametrize(
    "expected",
    [
        inventory(brewfile='brew "--debug"\n'),
        inventory(npm="--registry"),
        inventory(pipx="-e"),
        inventory(mas="--123"),
        inventory(python_scope="system", requirement="--index-url==bad"),
        inventory(app="-com.bad"),
        inventory(npm="bad\x00name"),
    ],
)
def test_provider_items_reject_option_like_or_control_data(
    tmp_path: Path, expected: Inventory
) -> None:
    # Given / When / Then
    with pytest.raises(RestoreInputError, match="unsafe|invalid"):
        _ = plan_restore(request(tmp_path, expected, inventory()))


def test_provider_parsing_preserves_legitimate_scoped_names(tmp_path: Path) -> None:
    # Given
    expected = inventory(npm="@scope/tool", pipx="zope.interface")

    # When
    plan = plan_restore(request(tmp_path, expected, inventory()))

    # Then
    assert [row.argv for row in plan.operations] == [
        ("/usr/bin/env", "npm", "install", "--global", "@scope/tool@1.0"),
        ("/usr/bin/env", "pipx", "install", "zope.interface==1.0"),
    ]
    assert plan.operations[0].key.endswith("@scope%2Ftool")


@pytest.mark.parametrize(
    "scope",
    [
        "../venv",
        "/tmp/venv",
        "unknown",
        "venv:../escape",
        "venv:/absolute",
        "venv:workspace/a/../b",
    ],
)
def test_python_scope_rejects_escape_or_unknown_before_journal(
    tmp_path: Path, scope: str
) -> None:
    # Given
    expected = inventory(python_scope=scope)

    # When / Then
    with pytest.raises(RestoreInputError, match="scope"):
        _ = plan_restore(request(tmp_path, expected, inventory()))
    assert not (tmp_path / "env").exists()


def test_user_python_scope_uses_exact_user_install_argv(tmp_path: Path) -> None:
    # Given
    expected = inventory(python_scope="user")

    # When
    plan = plan_restore(request(tmp_path, expected, inventory()))

    # Then
    assert plan.operations[0].argv == (
        "/usr/bin/env",
        "/usr/bin/python3",
        "-m",
        "pip",
        "install",
        "--user",
        "demo==1.0",
    )
    assert plan.operations[0].key == "restore/python/user/demo"


def test_safe_workspace_venv_scope_derives_executable_and_parseable_key(
    tmp_path: Path,
) -> None:
    # Given
    expected = inventory(
        python_scope="venv:workspace/demo/.venv",
        python_executable="/tmp/attacker/python",
    )

    # When
    plan = plan_restore(request(tmp_path, expected, inventory()))

    # Then
    assert plan.operations[0].argv == (
        "/usr/bin/env",
        "/workspace/demo/.venv/bin/python",
        "-m",
        "pip",
        "install",
        "demo==1.0",
    )
    assert (
        plan.operations[0].key == "restore/python/venv%3Aworkspace%2Fdemo%2F.venv/demo"
    )


def test_present_application_skips_manual_and_reports_restored(tmp_path: Path) -> None:
    # Given
    expected = inventory(app="com.example.app")
    plan = plan_restore(request(tmp_path, expected, expected))
    runner = Runner(success)

    # When
    report = execute_restore(
        plan,
        RestoreContext(
            JournalStore(tmp_path / "env", "bot-1", MANIFEST_ID),
            runner,
            lambda: expected,
            RestoreOptions(),
        ),
    )

    # Then
    assert runner.requests == []
    assert report.count(ReportClass.RESTORED) == 1
    assert report.count(ReportClass.MANUAL_ACTION) == 0


def test_stale_crash_lock_recovers_without_repeating_verified_success(
    tmp_path: Path,
) -> None:
    # Given
    expected = inventory(brewfile='brew "jq"\n')
    plan = plan_restore(request(tmp_path, expected, inventory()))
    captures = iter((inventory(), expected))
    runner = Runner(success)
    store = JournalStore(tmp_path / "env", "bot-1", MANIFEST_ID)
    _ = execute_restore(
        plan,
        RestoreContext(store, runner, lambda: next(captures), RestoreOptions()),
    )
    lock = {
        "schema_version": 1,
        "pid": 99_999_999,
        "hostname": socket.gethostname(),
        "process_identity": "crashed-owner",
        "started_at": datetime(2026, 8, 26, tzinfo=UTC).isoformat(),
    }
    _ = store.lock_path.write_text(json.dumps(lock))
    store.lock_path.chmod(0o600)

    # When
    report = execute_restore(
        plan,
        RestoreContext(store, runner, lambda: expected, RestoreOptions()),
    )

    # Then
    assert len(runner.requests) == 1
    assert report.exit_code == 0
    assert not store.lock_path.exists()
