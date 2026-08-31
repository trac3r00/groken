import errno
import json
import os
import re
import shutil
import subprocess
import tempfile
import tomllib
from dataclasses import dataclass
from datetime import date, datetime, time
from enum import StrEnum
from pathlib import Path
from typing import Final, NewType, TypeAlias

RoutineName = NewType("RoutineName", str)
EntryFilename = NewType("EntryFilename", str)
TomlValue: TypeAlias = (
    str
    | int
    | float
    | bool
    | date
    | datetime
    | time
    | list["TomlValue"]
    | dict[str, "TomlValue"]
)

_NAME_PATTERN: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*\Z")
_FIELDS: Final = frozenset({"name", "description", "events", "entry"})


class RoutineEvent(StrEnum):
    PRE_UPDATE = "pre-update"
    POST_UPDATE = "post-update"
    ENV_RESTORE = "env-restore"
    MANUAL = "manual"


@dataclass(frozen=True, slots=True)
class Routine:
    name: RoutineName
    description: str
    events: tuple[RoutineEvent, ...]
    directory: Path
    entry: Path


@dataclass(frozen=True, slots=True)
class RoutineTemplate:
    name: RoutineName
    description: str
    events: tuple[RoutineEvent, ...]
    script: str


class RoutineError(Exception):
    """Base class for routine store failures."""


@dataclass(frozen=True, slots=True)
class InvalidRoutineError(RoutineError):
    location: Path
    detail: str

    def __post_init__(self) -> None:
        Exception.__init__(self, f"{self.location}: {self.detail}")


@dataclass(frozen=True, slots=True)
class RoutineExistsError(RoutineError):
    name: RoutineName

    def __post_init__(self) -> None:
        Exception.__init__(self, f"routine {self.name!r} already exists")


@dataclass(frozen=True, slots=True)
class RoutineExecutionError(RoutineError):
    name: RoutineName
    detail: str

    def __post_init__(self) -> None:
        Exception.__init__(self, f"routine {self.name!r}: {self.detail}")


BUILTIN_TEMPLATES: Final = (
    RoutineTemplate(
        name=RoutineName("env-capture"),
        description="Capture the pod environment before an update.",
        events=(RoutineEvent.PRE_UPDATE, RoutineEvent.MANUAL),
        script=(
            "#!/bin/sh\n"
            "set -eu\n"
            "printf '%s\\n' 'groken: env-capture template is not configured; "
            "edit run.sh and replace this failure with your capture commands.' >&2\n"
            "exit 1\n"
        ),
    ),
    RoutineTemplate(
        name=RoutineName("env-restore"),
        description="Restore the pod environment after an update.",
        events=(RoutineEvent.ENV_RESTORE, RoutineEvent.MANUAL),
        script=(
            "#!/bin/sh\n"
            "set -eu\n"
            "printf '%s\\n' 'groken: env-restore template is not configured; "
            "edit run.sh and replace this failure with your restore commands.' >&2\n"
            "exit 1\n"
        ),
    ),
)
_GENERIC_SCRIPT: Final = "#!/bin/sh\nset -eu\n# TODO: implement this routine.\n"


def _config_dir() -> Path:
    return Path.home() / ".config" / "groken"


def _parse_name(value: str) -> RoutineName:
    if _NAME_PATTERN.fullmatch(value) is None:
        raise InvalidRoutineError(Path(value), "unsafe routine name")
    return RoutineName(value)


def _store_dir() -> Path:
    store = _config_dir() / "routines"
    if store.is_symlink() or (store.exists() and not store.is_dir()):
        raise InvalidRoutineError(store, "routine store must be a regular directory")
    return store


def _routine_dir(name: RoutineName) -> Path:
    return _store_dir() / name


def _load_metadata(path: Path) -> dict[str, TomlValue]:
    if path.is_symlink() or not path.is_file():
        raise InvalidRoutineError(path, "routine.toml must be a regular file")
    try:
        return tomllib.loads(path.read_text())
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise InvalidRoutineError(path, f"invalid TOML: {exc}") from exc


def load_routine(name: str) -> Routine:
    """Load and strictly parse one stored routine."""
    parsed_name = _parse_name(name)
    directory = _routine_dir(parsed_name)
    if directory.is_symlink() or not directory.is_dir():
        raise InvalidRoutineError(
            directory, "routine directory does not exist or is unsafe"
        )
    metadata_path = directory / "routine.toml"
    metadata = _load_metadata(metadata_path)
    unknown = sorted(set(metadata) - _FIELDS)
    if unknown:
        raise InvalidRoutineError(
            metadata_path, f"unknown field(s): {', '.join(unknown)}"
        )
    missing = sorted(_FIELDS - set(metadata))
    if missing:
        raise InvalidRoutineError(
            metadata_path, f"missing field(s): {', '.join(missing)}"
        )

    metadata_name = metadata["name"]
    description = metadata["description"]
    raw_events = metadata["events"]
    raw_entry = metadata["entry"]
    if not isinstance(metadata_name, str):
        raise InvalidRoutineError(metadata_path, "name must be a string")
    if metadata_name != parsed_name:
        raise InvalidRoutineError(
            metadata_path, "name must match the routine directory"
        )
    if not isinstance(description, str):
        raise InvalidRoutineError(metadata_path, "description must be a string")
    if not isinstance(raw_events, list):
        raise InvalidRoutineError(metadata_path, "events must be a list")
    events: list[RoutineEvent] = []
    for raw_event in raw_events:
        if not isinstance(raw_event, str):
            raise InvalidRoutineError(metadata_path, "each event must be a string")
        try:
            event = RoutineEvent(raw_event)
        except ValueError as exc:
            raise InvalidRoutineError(
                metadata_path, f"unknown event: {raw_event}"
            ) from exc
        if event in events:
            raise InvalidRoutineError(metadata_path, f"duplicate event: {raw_event}")
        events.append(event)
    if not isinstance(raw_entry, str):
        raise InvalidRoutineError(metadata_path, "entry must be a string")
    if (
        raw_entry in {"", ".", ".."}
        or Path(raw_entry).name != raw_entry
        or "\\" in raw_entry
    ):
        raise InvalidRoutineError(metadata_path, "entry must be a single filename")
    entry = directory / EntryFilename(raw_entry)
    if entry.is_symlink() or not entry.is_file():
        raise InvalidRoutineError(entry, "entry must be an existing regular file")
    return Routine(parsed_name, description, tuple(events), directory, entry)


def list_routines() -> tuple[Routine, ...]:
    """List stored routines in name order without creating or executing files."""
    store = _store_dir()
    if not store.exists():
        return ()
    names = sorted(
        path.name for path in store.iterdir() if not path.name.startswith(".")
    )
    return tuple(load_routine(name) for name in names)


def _write_file(path: Path, content: str, mode: int) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    os.fchmod(descriptor, mode)
    with os.fdopen(descriptor, "w") as stream:
        _ = stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def new_routine(name: str) -> Routine:
    """Atomically scaffold a new routine without replacing existing state."""
    parsed_name = _parse_name(name)
    store = _store_dir()
    target = store / parsed_name
    if target.exists() or target.is_symlink():
        raise RoutineExistsError(parsed_name)
    store.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(_config_dir(), 0o700)
    os.chmod(store, 0o700)
    temporary = Path(tempfile.mkdtemp(prefix=f".{parsed_name}.", dir=store))
    os.chmod(temporary, 0o700)
    published = False
    try:
        template = next(
            (
                candidate
                for candidate in BUILTIN_TEMPLATES
                if candidate.name == parsed_name
            ),
            RoutineTemplate(
                parsed_name,
                "User-defined routine.",
                (RoutineEvent.MANUAL,),
                _GENERIC_SCRIPT,
            ),
        )
        events = ", ".join(json.dumps(event.value) for event in template.events)
        metadata = (
            f"name = {json.dumps(parsed_name)}\n"
            f"description = {json.dumps(template.description)}\n"
            f"events = [{events}]\n"
            'entry = "run.sh"\n'
        )
        _write_file(temporary / "routine.toml", metadata, 0o600)
        _write_file(temporary / "run.sh", template.script, 0o700)
        try:
            os.rename(temporary, target)
        except OSError as exc:
            if exc.errno not in {errno.EEXIST, errno.ENOTEMPTY}:
                raise
            raise RoutineExistsError(parsed_name) from None
        published = True
    finally:
        if not published:
            shutil.rmtree(temporary, ignore_errors=True)
    return load_routine(parsed_name)


def edit_path(name: str) -> Path:
    """Return the entry path an editor should open without executing it."""
    return load_routine(name).entry


def run_routine(name: str, event: RoutineEvent = RoutineEvent.MANUAL) -> int:
    """Execute one declared routine event with an argv-only subprocess."""
    routine = load_routine(name)
    if event not in routine.events:
        raise RoutineExecutionError(
            routine.name, f"event {event.value!r} is not declared"
        )
    environment = os.environ.copy()
    environment.update(
        GROKEN_ROUTINE_NAME=routine.name,
        GROKEN_EVENT=event.value,
        GROKEN_CONFIG_DIR=str(_config_dir()),
    )
    try:
        return subprocess.run(
            [str(routine.entry)], env=environment, check=False
        ).returncode
    except OSError as exc:
        raise RoutineExecutionError(routine.name, str(exc)) from exc
