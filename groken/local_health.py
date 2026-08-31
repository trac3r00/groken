"""Read-only local compatibility health for doctor and status."""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final, TypedDict, cast

from . import installers, service
from .env_persistence import (
    CurrentManifest,
    CurrentManifestError,
    read_current_manifest,
)
from .routines import RoutineError, load_routine

_FRESH_LIMIT: Final = timedelta(hours=24)
_SEVERE_LIMIT: Final = timedelta(days=7)


@dataclass(frozen=True, slots=True)
class LocalCheck:
    name: str
    message: str
    warning: bool


class LocalCheckPayload(TypedDict):
    message: str
    warning: bool


class LocalStatusPayload(TypedDict):
    harnesses: LocalCheckPayload
    routines: LocalCheckPayload
    environment: LocalCheckPayload
    native: LocalCheckPayload
    lifecycle_swarm: str


def _payload(check: LocalCheck) -> LocalCheckPayload:
    return {"message": check.message, "warning": check.warning}


def inspect_harnesses() -> LocalCheck:
    """Report detected local harness names without touching their configuration."""
    names = tuple(sorted(installers.detected_agents()))
    message = f"{len(names)} detected ({', '.join(names)})" if names else "0 detected"
    return LocalCheck("harnesses", message, not names)


def inspect_routines() -> LocalCheck:
    """Strictly parse every visible stored routine without executing it."""
    root = Path.home() / ".config/groken/routines"
    try:
        metadata = root.lstat()
    except FileNotFoundError:
        return LocalCheck("routines", "0 healthy", False)
    if not stat.S_ISDIR(metadata.st_mode):
        return LocalCheck("routines", "0 healthy, store corrupt", True)
    healthy = 0
    corrupt = 0
    try:
        names = sorted(
            path.name for path in root.iterdir() if not path.name.startswith(".")
        )
    except OSError:
        return LocalCheck("routines", "0 healthy, store unreadable", True)
    for name in names:
        try:
            _ = load_routine(name)
        except (OSError, RoutineError):
            corrupt += 1
        else:
            healthy += 1
    message = f"{healthy} healthy"
    if corrupt:
        message += f", {corrupt} corrupt"
    return LocalCheck("routines", message, bool(corrupt))


def _cached_bot_id() -> str | None:
    path = Path.home() / ".config/groken/config.json"
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(metadata.st_mode):
        raise CurrentManifestError("cached Bot config is unsafe")
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor) as stream:
            value = cast("object", json.load(stream))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CurrentManifestError("cached Bot config is unreadable") from exc
    if not isinstance(value, dict):
        raise CurrentManifestError("cached Bot config is corrupt")
    bot_id = value.get("bot_id")
    if bot_id is None:
        return None
    if not isinstance(bot_id, str) or not bot_id:
        raise CurrentManifestError("cached Bot id is corrupt")
    return bot_id


def _manifest_source(current: CurrentManifest) -> str:
    try:
        directory = os.open(
            current.path,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            descriptor = os.open(
                "manifest.json",
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=directory,
            )
            with os.fdopen(descriptor) as stream:
                value = cast("object", json.load(stream))
        finally:
            os.close(directory)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CurrentManifestError("manifest source is unreadable") from exc
    if not isinstance(value, dict) or value.get("manifest_id") != current.manifest_id:
        raise CurrentManifestError("manifest source identity is corrupt")
    collectors = value.get("collectors")
    if collectors is None:
        return "unknown"
    if not isinstance(collectors, list):
        raise CurrentManifestError("manifest collectors are corrupt")
    source = "native"
    for collector in collectors:
        if not isinstance(collector, dict) or not isinstance(collector.get("id"), str):
            raise CurrentManifestError("manifest collector is corrupt")
        if collector["id"] == "chat":
            source = "chat"
    return source


def inspect_environment(now: datetime | None = None) -> LocalCheck:
    """Report the trusted current environment snapshot for the cached Bot."""
    observed_at = datetime.now(UTC) if now is None else now.astimezone(UTC)
    try:
        bot_id = _cached_bot_id()
        if bot_id is None:
            return LocalCheck("environment", "missing (no cached Bot)", True)
        current = read_current_manifest(Path.home() / ".config/groken/env", bot_id)
    except CurrentManifestError:
        return LocalCheck("environment", "corrupt (cached Bot snapshot)", True)
    if current is None:
        return LocalCheck("environment", "missing (cached Bot has no snapshot)", True)
    age = observed_at - current.captured_at
    if age < timedelta():
        return LocalCheck(
            "environment", "corrupt (snapshot timestamp is in the future)", True
        )
    hours = age.total_seconds() / 3600
    if age <= _FRESH_LIMIT:
        state = "fresh"
        warning = False
    elif age <= _SEVERE_LIMIT:
        state = "stale"
        warning = True
    else:
        state = "severe"
        warning = True
    try:
        source = _manifest_source(current)
    except CurrentManifestError:
        return LocalCheck("environment", "corrupt (snapshot source)", True)
    return LocalCheck(
        "environment",
        f"{state} (age={hours:.1f}h, source={source})",
        warning,
    )


def inspect_native() -> LocalCheck:
    """Report static native wait and launch-service configuration only."""
    wait = (
        "configured"
        if bool(os.environ.get("GROKEN_CONTROLLER_TOKEN"))
        else "unconfigured"
    )
    services = service.status()
    controller = "present" if services["controller"] else "absent"
    tunnel = "present" if services["tunnel"] else "absent"
    return LocalCheck(
        "native",
        f"wait {wait}; services controller={controller}, tunnel={tunnel}",
        wait == "unconfigured" or controller == "absent" or tunnel == "absent",
    )


def collect_local_status(now: datetime | None = None) -> LocalStatusPayload:
    """Collect every local check independently so one corrupt source cannot abort status."""
    checks: dict[str, LocalCheckPayload] = {}
    probes = (
        ("harnesses", inspect_harnesses),
        ("routines", inspect_routines),
        ("environment", lambda: inspect_environment(now)),
        ("native", inspect_native),
    )
    for name, probe in probes:
        try:
            checks[name] = _payload(probe())
        except (CurrentManifestError, OSError, RoutineError, TypeError, ValueError):
            checks[name] = {"message": "check failed", "warning": True}
    return {
        "harnesses": checks["harnesses"],
        "routines": checks["routines"],
        "environment": checks["environment"],
        "native": checks["native"],
        "lifecycle_swarm": "available",
    }


def render_local_status(local: LocalStatusPayload) -> str:
    """Render secret-safe local compatibility lines."""
    return "\n".join(
        (
            f"Harnesses: {local['harnesses']['message']}",
            f"Routines: {local['routines']['message']}",
            f"Environment: {local['environment']['message']}",
            f"Native: {local['native']['message']}",
            f"Lifecycle/swarm: {local['lifecycle_swarm']}",
        )
    )
