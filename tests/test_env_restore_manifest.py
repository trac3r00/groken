from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from groken.env_restore_manifest import (
    JsonValue,
    RestoreManifestError,
    load_inventory,
    load_latest_inventory,
)

MANIFEST_ID = "sha256:" + "c" * 64


def payload(
    bot_id: str = "bot-1", manifest_id: str = MANIFEST_ID
) -> dict[str, JsonValue]:
    return {
        "schema_version": 1,
        "manifest_id": manifest_id,
        "bot": {"id": bot_id, "name": "Demo"},
        "captured_at": datetime(2026, 8, 26, 12, tzinfo=UTC).isoformat(),
        "host": {"os": "Darwin", "os_version": "25", "arch": "arm64"},
        "collectors": [
            {
                "id": "brew",
                "status": "ok",
                "artifact": "artifacts/brew.raw",
                "sha256": "fixture",
                "command": ["brew"],
                "exit_code": 0,
                "error": None,
            }
        ],
        "inventory": {
            "brewfile": 'brew "jq"\n',
            "python": [],
            "npm": {"node_version": "v24", "prefix": "/usr/local", "packages": []},
            "pipx": [],
            "mas": [],
            "applications": [],
        },
    }


def write_manifest(root: Path) -> Path:
    target = root / "bot-1" / MANIFEST_ID
    target.mkdir(parents=True)
    _ = (target.parent / "current.json").write_text(
        json.dumps({"manifest_id": MANIFEST_ID})
    )
    artifact = target / "artifacts" / "brew.raw"
    artifact.parent.mkdir()
    _ = artifact.write_text('brew "jq"\n')
    _ = (target / "manifest.json").write_text(json.dumps(payload()))
    return target


def test_load_latest_inventory_uses_trusted_current_identity(tmp_path: Path) -> None:
    # Given
    root = tmp_path / "env"
    _ = write_manifest(root)

    # When
    loaded = load_latest_inventory(root, "bot-1")

    # Then
    assert loaded.manifest_id == MANIFEST_ID
    assert loaded.inventory.brewfile == 'brew "jq"\n'
    assert loaded.brewfile_path == loaded.path / "artifacts" / "brew.raw"


def test_load_inventory_rejects_symlink_manifest_root(tmp_path: Path) -> None:
    # Given
    real_root = tmp_path / "real"
    target = write_manifest(real_root)
    linked = tmp_path / "linked-manifest"
    linked.symlink_to(target, target_is_directory=True)

    # When / Then
    with pytest.raises(RestoreManifestError, match="root|directory|unsafe"):
        _ = load_inventory(linked, "bot-1", MANIFEST_ID)


def test_load_latest_rejects_symlink_environment_root(tmp_path: Path) -> None:
    # Given
    real_root = tmp_path / "real-env"
    _ = write_manifest(real_root)
    linked = tmp_path / "linked-env"
    linked.symlink_to(real_root, target_is_directory=True)

    # When / Then
    with pytest.raises(RestoreManifestError, match="root|directory|unsafe"):
        _ = load_latest_inventory(linked, "bot-1")


@pytest.mark.parametrize(
    "mutation", ["manifest-symlink", "foreign", "malformed", "unsafe-bot"]
)
def test_load_latest_inventory_rejects_unsafe_or_foreign_manifest(
    tmp_path: Path, mutation: str
) -> None:
    # Given
    root = tmp_path / "env"
    target = write_manifest(root)
    bot_id = "bot-1"
    if mutation == "manifest-symlink":
        source = target / "manifest.json"
        outside = tmp_path / "outside.json"
        _ = outside.write_bytes(source.read_bytes())
        source.unlink()
        source.symlink_to(outside)
    elif mutation == "foreign":
        _ = (target / "manifest.json").write_text(json.dumps(payload("bot-2")))
    elif mutation == "malformed":
        data = payload()
        del data["inventory"]
        _ = (target / "manifest.json").write_text(json.dumps(data))
    else:
        bot_id = "../bot-1"

    # When / Then
    with pytest.raises(RestoreManifestError, match="unsafe|identity|schema|manifest"):
        _ = load_latest_inventory(root, bot_id)
