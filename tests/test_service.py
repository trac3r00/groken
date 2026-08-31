import plistlib
import stat
from pathlib import Path
from typing import TypeGuard, cast

import pytest

from groken import service


def _is_object_dict(value: object) -> TypeGuard[dict[str, object]]:
    return isinstance(value, dict)


def _load_plist(path: Path) -> dict[str, object]:
    loaded = cast(object, plistlib.loads(path.read_bytes()))
    if not _is_object_dict(loaded):
        raise TypeError("expected plist dictionary")
    return loaded


def test_install_generates_fixture_shaped_plists_and_wrapper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    results = service.install()
    launch = tmp_path / "Library/LaunchAgents"
    controller = _load_plist(launch / "ai.bob.groken1-controller.plist")
    tunnel = _load_plist(launch / "ai.bob.groken1-tunnel.plist")
    assert results == {"controller": "installed", "tunnel": "installed"}
    assert controller == {
        "Label": "ai.bob.groken1-controller",
        "ProgramArguments": ["/bin/sh", str(tmp_path / ".local/bin/groken-controller-start.sh")],
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(tmp_path / ".local/state/groken1-controller.log"),
        "StandardErrorPath": str(tmp_path / ".local/state/groken1-controller.err.log"),
    }
    assert tunnel == {
        "Label": "ai.bob.groken1-tunnel",
        "ProgramArguments": [
            "/opt/homebrew/bin/cloudflared", "tunnel", "run", "--token-file",
            str(tmp_path / ".config/groken/groken1-tunnel.token"),
        ],
        "EnvironmentVariables": {"HOME": str(tmp_path)},
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(tmp_path / ".local/state/groken1-tunnel.log"),
        "StandardErrorPath": str(tmp_path / ".local/state/groken1-tunnel.err.log"),
    }
    wrapper = tmp_path / ".local/bin/groken-controller-start.sh"
    assert wrapper.read_text() == service.CONTROLLER_WRAPPER
    assert stat.S_IMODE(wrapper.stat().st_mode) == 0o700
    assert stat.S_IMODE((launch / "ai.bob.groken1-controller.plist").stat().st_mode) == 0o600
    assert stat.S_IMODE((launch / "ai.bob.groken1-tunnel.plist").stat().st_mode) == 0o600


def test_install_is_byte_stable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    _ = service.install()
    first = [(p, p.read_bytes()) for p in sorted((tmp_path / "Library/LaunchAgents").glob("*.plist"))]
    _ = service.install(adopt=True)
    second = [(p, p.read_bytes()) for p in sorted((tmp_path / "Library/LaunchAgents").glob("*.plist"))]
    assert first == second


def test_dry_run_makes_no_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    assert service.install(dry_run=True) == {"controller": "would install", "tunnel": "would install"}
    assert list(tmp_path.rglob("*")) == []


def test_existing_manual_install_can_be_skipped_or_adopted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def decline(_message: str) -> str:
        return "n"

    def accept(_message: str) -> str:
        return "y"

    monkeypatch.setenv("HOME", str(tmp_path))
    target = tmp_path / "Library/LaunchAgents/ai.bob.groken1-controller.plist"
    target.parent.mkdir(parents=True)
    _ = target.write_bytes(b"manual")
    assert service.install(prompt=decline)["controller"] == "skipped (manual install)"
    assert target.read_bytes() == b"manual"
    assert service.install(prompt=accept)["controller"] == "adopted"
    assert _load_plist(target)["Label"] == "ai.bob.groken1-controller"
