from __future__ import annotations

import json
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path
from queue import Queue
from threading import Barrier, Thread

import pytest

from groken.env_persistence import (
    CurrentManifestError,
    ManifestTree,
    MirrorTarget,
    PersistenceError,
    TreeFile,
    mirror_tree,
    read_current_manifest,
)


def tree(manifest_id: str = "sha256:one") -> ManifestTree:
    return ManifestTree(
        manifest_id,
        (
            TreeFile("artifacts/tool.raw", b"artifact"),
            TreeFile("manifest.json", b'{"manifest_id":"fixture"}\n'),
        ),
    )


def target(tmp_path: Path) -> MirrorTarget:
    return MirrorTarget(tmp_path / "env", "bot-1")


def write_current(bot_root: Path, bot_id: str = "bot-1") -> str:
    manifest_id = "sha256:" + "a" * 64
    manifest = bot_root / manifest_id
    manifest.mkdir(parents=True)
    _ = (bot_root / "current.json").write_text(json.dumps({"manifest_id": manifest_id}))
    _ = (manifest / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "manifest_id": manifest_id,
                "bot": {"id": bot_id, "name": "Demo"},
                "captured_at": datetime(2026, 8, 26, 12, tzinfo=UTC).isoformat(),
            }
        )
    )
    return manifest_id


@pytest.mark.parametrize("selector", ["traversal", "absolute"])
def test_read_current_manifest_rejects_bot_id_that_can_escape_root(
    tmp_path: Path,
    selector: str,
) -> None:
    # Given
    root = tmp_path / "env"
    root.mkdir()
    outside = tmp_path / "outside"
    bot_id = "../outside" if selector == "traversal" else str(outside)
    _ = write_current(outside, bot_id)

    # When / Then
    with pytest.raises(CurrentManifestError, match="unsafe bot id"):
        _ = read_current_manifest(root, bot_id)


@pytest.mark.parametrize(
    "mutation",
    [
        "bot-symlink",
        "bot-file",
        "current-symlink",
        "current-directory",
        "manifest-symlink",
        "manifest-file",
    ],
)
def test_read_current_manifest_rejects_symlink_or_nonregular_path(
    tmp_path: Path,
    mutation: str,
) -> None:
    # Given
    root = tmp_path / "env"
    root.mkdir()
    bot_root = root / "bot-1"
    outside = tmp_path / "outside"
    if mutation == "bot-symlink":
        _ = write_current(outside)
        bot_root.symlink_to(outside, target_is_directory=True)
    elif mutation == "bot-file":
        _ = bot_root.write_text("not a directory")
    else:
        manifest_id = write_current(bot_root)
        current = bot_root / "current.json"
        manifest = bot_root / manifest_id
        if mutation == "current-symlink":
            outside_current = tmp_path / "outside-current.json"
            _ = outside_current.write_bytes(current.read_bytes())
            current.unlink()
            _ = current.symlink_to(outside_current)
        elif mutation == "current-directory":
            current.unlink()
            current.mkdir()
        elif mutation == "manifest-symlink":
            outside_manifest = tmp_path / "outside-manifest"
            _ = manifest.rename(outside_manifest)
            _ = manifest.symlink_to(outside_manifest, target_is_directory=True)
        else:
            shutil.rmtree(manifest)
            _ = manifest.write_text("not a directory")

    # When / Then
    with pytest.raises(CurrentManifestError, match="unsafe|regular|directory"):
        _ = read_current_manifest(root, "bot-1")


def test_read_current_manifest_accepts_safe_regular_tree(tmp_path: Path) -> None:
    # Given
    root = tmp_path / "env"
    manifest_id = write_current(root / "bot-1")

    # When
    current = read_current_manifest(root, "bot-1")

    # Then
    assert current is not None
    assert current.manifest_id == manifest_id


@pytest.mark.parametrize("mutation", ["symlink", "directory", "missing", "extra"])
def test_env_capture_collision_rejects_non_exact_regular_tree(
    tmp_path: Path, mutation: str
) -> None:
    # Given
    mirror = target(tmp_path)
    published = mirror_tree(mirror, tree())
    artifact = published / "artifacts/tool.raw"
    if mutation == "symlink":
        outside = tmp_path / "outside"
        _ = outside.write_bytes(b"artifact")
        artifact.unlink()
        artifact.symlink_to(outside)
    elif mutation == "directory":
        artifact.unlink()
        artifact.mkdir()
    elif mutation == "missing":
        artifact.unlink()
    else:
        _ = (published / "extra").write_text("unexpected")

    # When / Then
    with pytest.raises(PersistenceError, match="collision"):
        _ = mirror_tree(mirror, tree())


def test_env_capture_collision_accepts_identical_regular_tree(tmp_path: Path) -> None:
    # Given
    mirror = target(tmp_path)
    expected = mirror_tree(mirror, tree())

    # When
    actual = mirror_tree(mirror, tree())

    # Then
    assert actual == expected


def test_env_capture_interrupted_current_preserves_prior_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given
    mirror = target(tmp_path)
    _ = mirror_tree(mirror, tree())
    current = mirror.local_root / mirror.bot_id / "current.json"
    original = current.read_bytes()
    real_replace = os.replace

    def interrupt(source: str | Path, destination: str | Path) -> None:
        if Path(destination) == current:
            raise OSError("interrupted current update")
        real_replace(source, destination)

    monkeypatch.setattr("groken.env_persistence.os.replace", interrupt)

    # When / Then
    with pytest.raises(OSError, match="interrupted"):
        _ = mirror_tree(mirror, tree("sha256:two"))
    assert current.read_bytes() == original
    assert list(current.parent.glob(".current-*.tmp")) == []


def test_env_capture_concurrent_identical_publish_is_consistent(tmp_path: Path) -> None:
    # Given
    mirror = target(tmp_path)
    barrier = Barrier(3)
    results: Queue[Path | Exception] = Queue()

    def publish() -> None:
        _ = barrier.wait(timeout=5)
        try:
            results.put(mirror_tree(mirror, tree()))
        except Exception as exc:  # noqa: BLE001 - thread boundary returns the failure to the test.
            results.put(exc)

    threads = (Thread(target=publish), Thread(target=publish))
    for thread in threads:
        thread.start()

    # When
    _ = barrier.wait(timeout=5)
    for thread in threads:
        thread.join(timeout=5)

    # Then
    outcomes = (results.get(), results.get())
    assert all(isinstance(outcome, Path) for outcome in outcomes)
    assert json.loads(
        (mirror.local_root / mirror.bot_id / "current.json").read_text()
    ) == {"manifest_id": "sha256:one"}
