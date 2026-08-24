"""Generate the two groken1 launchd services without requiring root."""

from __future__ import annotations

import os
import plistlib
from pathlib import Path
from typing import Callable

CONTROLLER_WRAPPER = '''#!/bin/sh
set -a
. "$HOME/.config/groken/controller.env"
set +a
exec "$HOME/groken/.venv/bin/groken-controller" --host 127.0.0.1 --port 18766
'''

CONTROLLER_LABEL = "ai.bob.groken1-controller"
TUNNEL_LABEL = "ai.bob.groken1-tunnel"


def _home() -> Path:
    return Path(os.environ.get("HOME", str(Path.home()))).expanduser()


def _paths(home: Path) -> tuple[Path, Path, Path]:
    agents = home / "Library" / "LaunchAgents"
    return (
        agents / f"{CONTROLLER_LABEL}.plist",
        agents / f"{TUNNEL_LABEL}.plist",
        home / ".local" / "bin" / "groken-controller-start.sh",
    )


def _plists(home: Path) -> tuple[dict[str, object], dict[str, object]]:
    state = home / ".local" / "state"
    return (
        {
            "Label": CONTROLLER_LABEL,
            "ProgramArguments": ["/bin/sh", str(home / ".local/bin/groken-controller-start.sh")],
            "RunAtLoad": True,
            "KeepAlive": True,
            "StandardOutPath": str(state / "groken1-controller.log"),
            "StandardErrorPath": str(state / "groken1-controller.err.log"),
        },
        {
            "Label": TUNNEL_LABEL,
            "ProgramArguments": [
                "/opt/homebrew/bin/cloudflared", "tunnel", "run", "--token-file",
                str(home / ".config/groken/groken1-tunnel.token"),
            ],
            "EnvironmentVariables": {"HOME": str(home)},
            "RunAtLoad": True,
            "KeepAlive": True,
            "StandardOutPath": str(state / "groken1-tunnel.log"),
            "StandardErrorPath": str(state / "groken1-tunnel.err.log"),
        },
    )


def _is_generated(path: Path, expected: dict[str, object]) -> bool:
    try:
        return plistlib.loads(path.read_bytes()) == expected
    except (OSError, plistlib.InvalidFileException, ValueError):
        return False


def _write(path: Path, data: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    path.chmod(mode)


def install(*, dry_run: bool = False, adopt: bool | None = None,
            prompt: Callable[[str], str] | None = None) -> dict[str, str]:
    """Install controller and tunnel plists, asking before adopting existing files."""
    home = _home()
    controller_path, tunnel_path, wrapper_path = _paths(home)
    controller, tunnel = _plists(home)
    results: dict[str, str] = {}
    for name, path, payload in (("controller", controller_path, controller), ("tunnel", tunnel_path, tunnel)):
        if path.exists():
            if _is_generated(path, payload):
                outcome = "installed"
            else:
                decision = adopt
                if decision is None and not dry_run:
                    ask = prompt or input
                    answer = ask(f"Adopt existing manual {name} launchd install at {path}? [y/N] ")
                    decision = answer.strip().lower() in {"y", "yes"}
                if decision is not True:
                    results[name] = "skipped (manual install)"
                    continue
                outcome = "adopted"
        else:
            outcome = "would install" if dry_run else "installed"
        if not dry_run:
            _write(path, plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=False), 0o600)
        results[name] = outcome
    if not dry_run and not wrapper_path.exists():
        _write(wrapper_path, CONTROLLER_WRAPPER.encode(), 0o700)
    return results


def status() -> dict[str, bool]:
    """Return presence of only groken1's generated launch agent files."""
    controller, tunnel, _ = _paths(_home())
    return {"controller": controller.exists(), "tunnel": tunnel.exists()}


def uninstall(*, dry_run: bool = False) -> dict[str, str]:
    """Remove only groken1 launch agents and its generated wrapper."""
    controller, tunnel, wrapper = _paths(_home())
    results = {}
    for name, path in (("controller", controller), ("tunnel", tunnel), ("wrapper", wrapper)):
        if path.exists():
            results[name] = "would remove" if dry_run else "removed"
            if not dry_run:
                path.unlink()
        else:
            results[name] = "not present"
    return results
