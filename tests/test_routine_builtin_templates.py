import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from groken.routines import RoutineEvent, new_routine


@dataclass(frozen=True, slots=True)
class BuiltinTemplateCase:
    name: str
    events: tuple[RoutineEvent, ...]


@pytest.mark.parametrize(
    "case",
    [
        BuiltinTemplateCase(
            "env-capture", (RoutineEvent.PRE_UPDATE, RoutineEvent.MANUAL)
        ),
        BuiltinTemplateCase(
            "env-restore", (RoutineEvent.ENV_RESTORE, RoutineEvent.MANUAL)
        ),
    ],
    ids=["env-capture", "env-restore"],
)
def test_builtin_template_fails_until_configured_when_newly_scaffolded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: BuiltinTemplateCase,
) -> None:
    # Given
    monkeypatch.setenv("HOME", str(tmp_path))
    routine = new_routine(case.name)

    # When
    result = subprocess.run(
        [routine.entry], capture_output=True, text=True, check=False
    )

    # Then
    assert routine.name == case.name
    assert routine.events == case.events
    assert stat.S_IMODE((routine.directory / "routine.toml").stat().st_mode) == 0o600
    assert stat.S_IMODE(routine.entry.stat().st_mode) == 0o700
    assert result.returncode != 0
    assert case.name in result.stderr
    assert "not configured" in result.stderr
