from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from groken import capabilities, doctor, installers, local_health
from groken.capabilities import CommandRisk

NOW = datetime(2026, 8, 26, 12, tzinfo=UTC)
_MALICIOUS_ROUTINE_NAME = (
    "bad-TOKEN_task9_secret-\\private\\path-\x1b[31m\nnewline-marker"
)


def _local_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(installers, "HOME", tmp_path)
    config = tmp_path / ".config/groken/config.json"
    config.parent.mkdir(parents=True)
    _ = config.write_text(json.dumps({"bot_id": "bot-1", "bot_name": "Demo"}))
    return config


def _write_manifest(root: Path, captured_at: datetime, source: str = "native") -> Path:
    manifest_id = "sha256:" + "a" * 64
    bot_root = root / ".config/groken/env/bot-1"
    manifest = bot_root / manifest_id
    manifest.mkdir(parents=True)
    _ = (bot_root / "current.json").write_text(json.dumps({"manifest_id": manifest_id}))
    collector_id = "chat" if source == "chat" else "brew"
    _ = (manifest / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "manifest_id": manifest_id,
                "bot": {"id": "bot-1", "name": "Demo"},
                "captured_at": captured_at.isoformat(),
                "collectors": [{"id": collector_id}],
            }
        )
    )
    return bot_root


def _write_routine(home: Path, name: str, *, valid: bool) -> None:
    routine = home / ".config/groken/routines" / name
    routine.mkdir(parents=True)
    metadata = (
        f'name = "{name}"\ndescription = "fixture"\nevents = ["manual"]\nentry = "run.sh"\n'
        if valid
        else 'name = "unterminated'
    )
    _ = (routine / "routine.toml").write_text(metadata)
    _ = (routine / "run.sh").write_text("#!/bin/sh\nexit 0\n")


def _tree_hashes(root: Path) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        relative = str(path.relative_to(root))
        if path.is_symlink():
            hashes[relative] = f"symlink:{os.readlink(path)}"
        elif path.is_file():
            hashes[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
        else:
            hashes[relative] = "directory"
    return hashes


def test_env_snapshot_age_boundaries_and_capture_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ = _local_home(tmp_path, monkeypatch)
    bot_root = _write_manifest(tmp_path, NOW - timedelta(hours=24), "chat")

    exact_24h = local_health.inspect_environment(NOW)
    shutil.rmtree(bot_root)
    _ = _write_manifest(tmp_path, NOW - timedelta(days=7), "native")
    exact_7d = local_health.inspect_environment(NOW)
    manifest = (
        tmp_path / ".config/groken/env/bot-1" / ("sha256:" + "a" * 64) / "manifest.json"
    )
    payload = json.loads(manifest.read_text())
    payload["captured_at"] = (NOW - timedelta(days=7, microseconds=1)).isoformat()
    _ = manifest.write_text(json.dumps(payload))
    over_7d = local_health.inspect_environment(NOW)

    assert exact_24h.message == "fresh (age=24.0h, source=chat)"
    assert exact_24h.warning is False
    assert exact_7d.message == "stale (age=168.0h, source=native)"
    assert exact_7d.warning is True
    assert over_7d.message.startswith("severe (age=168.0h, source=native)")
    assert over_7d.warning is True


@pytest.mark.parametrize("state", ["missing", "corrupt", "symlink"])
def test_env_snapshot_missing_corrupt_and_symlink_are_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    _ = _local_home(tmp_path, monkeypatch)
    if state == "corrupt":
        bot_root = tmp_path / ".config/groken/env/bot-1"
        bot_root.mkdir(parents=True)
        _ = (bot_root / "current.json").write_text("not-json")
    elif state == "symlink":
        outside = tmp_path / "outside"
        _ = _write_manifest(outside, NOW)
        target = tmp_path / ".config/groken/env/bot-1"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.symlink_to(
            outside / ".config/groken/env/bot-1", target_is_directory=True
        )
    before = _tree_hashes(tmp_path)

    check = local_health.inspect_environment(NOW)

    assert check.message.startswith("missing" if state == "missing" else "corrupt")
    assert check.warning is True
    assert _tree_hashes(tmp_path) == before


def test_doctor_local_subchecks_continue_after_named_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(doctor, "load_tokens", lambda: None)
    monkeypatch.setattr(
        doctor, "GatewayManager", lambda: (_ for _ in ()).throw(RuntimeError())
    )
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 1, ""),
    )
    monkeypatch.setattr(
        doctor, "inspect_harnesses", lambda: (_ for _ in ()).throw(RuntimeError())
    )
    monkeypatch.setattr(
        doctor,
        "inspect_routines",
        lambda: local_health.LocalCheck("routines", "2 healthy", False),
    )
    monkeypatch.setattr(
        doctor,
        "inspect_environment",
        lambda: local_health.LocalCheck("env", "missing", True),
    )
    monkeypatch.setattr(
        doctor,
        "inspect_native",
        lambda: local_health.LocalCheck(
            "native", "wait configured; services absent", True
        ),
    )

    assert doctor.run_doctor() == 1
    output = capsys.readouterr().out
    assert "8a harnesses: WARN" in output
    assert "8b routines: 2 healthy" in output
    assert "8c env: WARN" in output
    assert "8d native: WARN" in output
    assert output.index("8a harnesses") < output.index("8d native")


def test_doctor_never_renders_malicious_corrupt_routine_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(installers, "HOME", tmp_path)
    routine = tmp_path / ".config/groken/routines" / _MALICIOUS_ROUTINE_NAME
    routine.mkdir(parents=True)
    _ = (routine / "routine.toml").write_text('name = "unterminated')
    _ = (routine / "run.sh").write_text("#!/bin/sh\nexit 0\n")
    monkeypatch.setattr(doctor, "load_tokens", lambda: None)
    monkeypatch.setattr(
        doctor, "GatewayManager", lambda: (_ for _ in ()).throw(RuntimeError())
    )
    monkeypatch.setattr(
        doctor.httpx, "Client", lambda **_kwargs: (_ for _ in ()).throw(OSError())
    )
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 1, ""),
    )
    before = _tree_hashes(tmp_path)

    assert doctor.run_doctor() == 1
    output = capsys.readouterr().out
    assert "8b routines: WARN — 0 healthy, 1 corrupt" in output
    assert _MALICIOUS_ROUTINE_NAME not in output
    assert "TOKEN_task9_secret" not in output
    assert "private\\path" not in output
    assert "newline-marker" not in output
    assert "\x1b" not in output
    assert _tree_hashes(tmp_path) == before


def test_status_survives_corrupt_routine_and_reports_local_features(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ = _local_home(tmp_path, monkeypatch)
    _write_routine(tmp_path, "good", valid=True)
    _write_routine(tmp_path, "bad", valid=False)
    _ = _write_manifest(tmp_path, NOW - timedelta(hours=1), "native")
    monkeypatch.setattr(installers, "detected_agents", lambda: ["hermes", "omo"])
    monkeypatch.setenv("GROKEN_CONTROLLER_TOKEN", "must-not-leak")

    local = local_health.collect_local_status(NOW)
    text = local_health.render_local_status(local)

    assert "Harnesses: 2 detected (hermes, omo)" in text
    assert "Routines: 1 healthy, 1 corrupt" in text
    assert "Environment: fresh (age=1.0h, source=native)" in text
    assert "Native: wait configured" in text
    assert "Lifecycle/swarm: available" in text
    assert "must-not-leak" not in text


def test_local_diagnostics_never_write_or_execute_routines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ = _local_home(tmp_path, monkeypatch)
    _write_routine(tmp_path, "good", valid=True)
    _ = _write_manifest(tmp_path, NOW, "native")
    sentinel = tmp_path / "executed"
    script = tmp_path / ".config/groken/routines/good/run.sh"
    _ = script.write_text(f"#!/bin/sh\ntouch {sentinel}\n")
    before = _tree_hashes(tmp_path)

    _ = local_health.collect_local_status(NOW)

    assert _tree_hashes(tmp_path) == before
    assert not sentinel.exists()


def test_capability_risks_and_local_feature_manifest_are_stable() -> None:
    by_name = {spec.name: spec.risk for spec in capabilities.GATEWAY_COMMANDS}
    manifest = capabilities.capability_manifest(include_commands=False)

    assert by_name["autoUpdateBoxNow"] is CommandRisk.DESTRUCTIVE
    assert {
        by_name[name] for name in ("createAgent", "duplicateAgent", "updateAgent")
    } == {CommandRisk.MUTATING}
    assert {
        by_name[name]
        for name in (
            "getForeverBoxStatus",
            "getSharingState",
            "listAgents",
            "listBoxMcpServers",
        )
    } == {CommandRisk.READ_ONLY}
    assert manifest["local_features"] == {
        "harnesses": {"detect": True, "install": True, "uninstall": True},
        "routines": {"list": True, "new": True, "edit": True, "run": True},
        "env": {"capture": True, "restore": True, "current_snapshot": True},
        "update": {"status": True, "manual_trigger": True, "scheduled": False},
        "swarm": {"send": True, "rooms": True},
        "mcp": {"available": True, "confirmation_gates": True},
    }


def test_native_and_service_diagnostics_are_static_and_secret_safe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ = _local_home(tmp_path, monkeypatch)
    monkeypatch.setenv("GROKEN_CONTROLLER_TOKEN", "secret-value")
    launch = tmp_path / "Library/LaunchAgents"
    launch.mkdir(parents=True)
    _ = (launch / "ai.bob.groken1-controller.plist").write_bytes(b"fixture")
    before = _tree_hashes(tmp_path)

    check = local_health.inspect_native()

    assert (
        check.message == "wait configured; services controller=present, tunnel=absent"
    )
    assert "secret-value" not in check.message
    assert _tree_hashes(tmp_path) == before
