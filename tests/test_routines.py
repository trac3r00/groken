import stat
import sys
from pathlib import Path
from queue import Queue
from threading import Barrier, Thread

import pytest

from groken import cli
from groken.routines import (
    BUILTIN_TEMPLATES,
    RoutineError,
    RoutineEvent,
    edit_path,
    list_routines,
    load_routine,
    new_routine,
    run_routine,
)


def invoke_cli(monkeypatch: pytest.MonkeyPatch, argv: list[str]) -> int:
    monkeypatch.setattr(sys, "argv", ["groken", *argv])
    try:
        cli.main()
    except SystemExit as exc:
        assert isinstance(exc.code, int)
        return exc.code
    return 0


def write_routine(home: Path, name: str, metadata: str) -> Path:
    directory = home / ".config" / "groken" / "routines" / name
    directory.mkdir(parents=True)
    _ = (directory / "routine.toml").write_text(metadata)
    script = directory / "run.sh"
    _ = script.write_text("#!/bin/sh\nexit 0\n")
    _ = script.chmod(0o700)
    return directory


def test_new_scaffolds_loadable_private_routine_when_name_is_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given
    monkeypatch.setenv("HOME", str(tmp_path))

    # When
    routine = new_routine("demo")

    # Then
    assert load_routine("demo") == routine
    assert routine.name == "demo"
    assert routine.events == (RoutineEvent.MANUAL,)
    assert stat.S_IMODE((tmp_path / ".config" / "groken").stat().st_mode) == 0o700
    assert stat.S_IMODE(routine.directory.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(routine.directory.stat().st_mode) == 0o700
    assert stat.S_IMODE((routine.directory / "routine.toml").stat().st_mode) == 0o600
    assert stat.S_IMODE(routine.entry.stat().st_mode) == 0o700


def test_new_rejects_duplicate_without_overwriting_existing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given
    monkeypatch.setenv("HOME", str(tmp_path))
    routine = new_routine("demo")
    _ = routine.entry.write_text("keep me")

    # When / Then
    with pytest.raises(RoutineError, match="already exists"):
        _ = new_routine("demo")
    assert routine.entry.read_text() == "keep me"


def test_builtin_templates_are_listed_without_files_or_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Given
    monkeypatch.setenv("HOME", str(tmp_path))

    # When
    code = invoke_cli(monkeypatch, ["routine", "list"])

    # Then
    assert code == 0
    assert {template.name for template in BUILTIN_TEMPLATES} == {
        "env-capture",
        "env-restore",
    }
    assert {"env-capture", "env-restore"} <= set(capsys.readouterr().out.splitlines())
    assert list_routines() == ()
    assert not (tmp_path / ".config").exists()


def test_run_uses_argv_and_exports_routine_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    # Given
    monkeypatch.setenv("HOME", str(tmp_path))
    routine = new_routine("demo")
    _ = routine.entry.write_text(
        '#!/bin/sh\nprintf \'%s|%s|%s\\n\' "$GROKEN_ROUTINE_NAME" "$GROKEN_EVENT" "$GROKEN_CONFIG_DIR"\n'
    )
    _ = routine.entry.chmod(0o700)

    # When
    code = run_routine("demo", RoutineEvent.MANUAL)

    # Then
    assert code == 0
    assert capfd.readouterr().out == f"demo|manual|{tmp_path}/.config/groken\n"


def test_cli_forwards_explicit_event_without_executing_undeclared_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Given
    monkeypatch.setenv("HOME", str(tmp_path))
    routine = new_routine("demo")
    sentinel = tmp_path / "executed"
    _ = routine.entry.write_text(f"#!/bin/sh\ntouch {sentinel}\n")
    _ = routine.entry.chmod(0o700)

    # When
    code = invoke_cli(monkeypatch, ["routine", "run", "demo", "--event", "pre-update"])

    # Then
    assert code == 1
    assert "event 'pre-update' is not declared" in capsys.readouterr().err
    assert not sentinel.exists()


@pytest.mark.parametrize(
    ("metadata", "message"),
    [
        (
            'name = "demo"\ndescription = "x"\nevents = ["manual"]\nentry = "run.sh"\nextra = true\n',
            "unknown field.*extra",
        ),
        (
            'name = 3\ndescription = "x"\nevents = ["manual"]\nentry = "run.sh"\n',
            "name.*string",
        ),
        (
            'name = "demo"\ndescription = 3\nevents = ["manual"]\nentry = "run.sh"\n',
            "description.*string",
        ),
        (
            'name = "demo"\ndescription = "x"\nevents = "manual"\nentry = "run.sh"\n',
            "events.*list",
        ),
        (
            'name = "demo"\ndescription = "x"\nevents = [3]\nentry = "run.sh"\n',
            "event.*string",
        ),
        (
            'name = "demo"\ndescription = "x"\nevents = ["automatic"]\nentry = "run.sh"\n',
            "unknown event.*automatic",
        ),
        (
            'name = "demo"\ndescription = "x"\nevents = ["manual"]\nentry = 3\n',
            "entry.*string",
        ),
        (
            'name = "demo"\ndescription = "x"\nevents = ["manual"]\nentry = "../run.sh"\n',
            "single filename",
        ),
    ],
)
def test_load_rejects_invalid_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    metadata: str,
    message: str,
) -> None:
    # Given
    monkeypatch.setenv("HOME", str(tmp_path))
    _ = write_routine(tmp_path, "demo", metadata)

    # When / Then
    with pytest.raises(RoutineError, match=message):
        _ = load_routine("demo")


def test_load_rejects_malformed_toml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given
    monkeypatch.setenv("HOME", str(tmp_path))
    _ = write_routine(tmp_path, "demo", 'name = "unterminated')

    # When / Then
    with pytest.raises(RoutineError, match="invalid TOML"):
        _ = load_routine("demo")


@pytest.mark.parametrize("entry_kind", ["missing", "directory", "symlink"])
def test_load_rejects_entry_that_is_not_a_regular_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry_kind: str
) -> None:
    # Given
    monkeypatch.setenv("HOME", str(tmp_path))
    directory = write_routine(
        tmp_path,
        "demo",
        'name = "demo"\ndescription = "x"\nevents = ["manual"]\nentry = "run.sh"\n',
    )
    (directory / "run.sh").unlink()
    if entry_kind == "directory":
        (directory / "run.sh").mkdir()
    if entry_kind == "symlink":
        _ = (directory / "target.sh").write_text("#!/bin/sh\n")
        (directory / "run.sh").symlink_to("target.sh")

    # When / Then
    with pytest.raises(RoutineError, match="regular file"):
        _ = load_routine("demo")


@pytest.mark.parametrize("name", ["../escape", "with/slash", ".hidden", "two words"])
def test_api_rejects_unsafe_routine_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    # Given
    monkeypatch.setenv("HOME", str(tmp_path))

    # When / Then
    with pytest.raises(RoutineError, match="unsafe routine name"):
        _ = new_routine(name)


def test_list_new_and_edit_never_execute_an_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Given
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("EDITOR", raising=False)
    routine = new_routine("demo")
    sentinel = tmp_path / "executed"
    _ = routine.entry.write_text(f"#!/bin/sh\ntouch {sentinel}\n")
    _ = routine.entry.chmod(0o700)

    # When
    list_code = invoke_cli(monkeypatch, ["routine", "list"])
    new_code = invoke_cli(monkeypatch, ["routine", "new", "other"])
    edit_code = invoke_cli(monkeypatch, ["routine", "edit", "demo"])

    # Then
    assert (list_code, new_code, edit_code) == (0, 0, 0)
    assert str(edit_path("demo")) in capsys.readouterr().out.splitlines()
    assert not sentinel.exists()


def test_cli_error_names_unknown_field_without_writes_or_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Given
    monkeypatch.setenv("HOME", str(tmp_path))
    directory = write_routine(
        tmp_path,
        "bad",
        'name = "bad"\ndescription = "x"\nevents = ["manual"]\nentry = "run.sh"\nunknown = true\n',
    )
    sentinel = tmp_path / "executed"
    _ = (directory / "run.sh").write_text(f"#!/bin/sh\ntouch {sentinel}\n")
    before = {path: path.read_bytes() for path in directory.iterdir()}

    # When
    code = invoke_cli(monkeypatch, ["routine", "run", "bad"])

    # Then
    assert code == 1
    assert "unknown field" in capsys.readouterr().err
    assert {path: path.read_bytes() for path in directory.iterdir()} == before
    assert not sentinel.exists()


def test_concurrent_new_publishes_one_complete_scaffold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given
    monkeypatch.setenv("HOME", str(tmp_path))
    barrier = Barrier(3)
    outcomes: Queue[str] = Queue()

    def create() -> None:
        _ = barrier.wait(timeout=5)
        try:
            _ = new_routine("demo")
        except RoutineError:
            outcomes.put("exists")
        else:
            outcomes.put("created")

    threads = (Thread(target=create), Thread(target=create))
    for thread in threads:
        thread.start()

    # When
    _ = barrier.wait(timeout=5)
    for thread in threads:
        thread.join(timeout=5)

    # Then
    assert all(not thread.is_alive() for thread in threads)
    assert sorted((outcomes.get(), outcomes.get())) == ["created", "exists"]
    assert load_routine("demo").entry.is_file()
