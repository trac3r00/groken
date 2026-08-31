import os
import subprocess
from pathlib import Path
from typing import NoReturn

import pytest

from groken import checksum


def _force_uuid_fallback(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    def boom(*_args: object, **_kwargs: object) -> NoReturn:
        raise OSError("no ioreg")

    monkeypatch.setattr(subprocess, "run", boom)
    state_dir = tmp_path / "state"
    monkeypatch.setattr(checksum, "_STATE_DIR", state_dir)
    monkeypatch.setattr(checksum, "_MACHINE_ID_FILE", state_dir / "machine_id")
    return state_dir / "machine_id"


def test_machine_id_generates_and_persists(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    mid_file = _force_uuid_fallback(monkeypatch, tmp_path)
    first = checksum.get_machine_id()
    assert mid_file.read_text() == first
    assert (mid_file.stat().st_mode & 0o777) == 0o600
    assert checksum.get_machine_id() == first


def test_machine_id_fallback_uses_atomic_private_replace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    mid_file = _force_uuid_fallback(monkeypatch, tmp_path)
    real_replace = os.replace
    replacements: list[tuple[Path, Path]] = []
    fsync_calls: list[int] = []

    def replace(
        source: str | os.PathLike[str],
        destination: str | os.PathLike[str],
    ) -> None:
        replacements.append((Path(source), Path(destination)))
        real_replace(source, destination)

    monkeypatch.setattr(os, "replace", replace)
    monkeypatch.setattr(os, "fsync", fsync_calls.append)

    _ = checksum.get_machine_id()

    assert len(replacements) == 1
    temporary, destination = replacements[0]
    assert temporary.parent == mid_file.parent
    assert destination == mid_file
    assert fsync_calls
    assert (mid_file.stat().st_mode & 0o777) == 0o600


def test_machine_id_reads_existing_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    mid_file = _force_uuid_fallback(monkeypatch, tmp_path)
    mid_file.parent.mkdir(parents=True)
    _ = mid_file.write_text("stored-id\n")
    assert checksum.get_machine_id() == "stored-id"
