import json
import os
from pathlib import Path

import pytest

from groken import config


def test_default_bot_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.delenv("GROKEN_BOT_NAME", raising=False)
    assert config.bot_name() == "groken"


def test_env_override_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setenv("GROKEN_BOT_NAME", "custom-bot")
    assert config.bot_name() == "custom-bot"


@pytest.mark.parametrize(
    ("content", "reason"),
    [("{broken", "malformed JSON"), (json.dumps(["config"]), "expected a JSON object")],
)
def test_load_config_raises_typed_actionable_error_when_state_is_invalid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    content: str,
    reason: str,
) -> None:
    # Given
    config_file = tmp_path / "config.json"
    _ = config_file.write_text(content)
    monkeypatch.setattr(config, "CONFIG_FILE", config_file)

    # When
    with pytest.raises(config.ConfigStateError) as raised:
        _ = config.load_config()

    # Then
    assert raised.value.path == config_file
    assert raised.value.reason == reason
    assert "groken configure" in str(raised.value)


def test_save_config_uses_atomic_private_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "config.json"
    monkeypatch.setattr(config, "CONFIG_FILE", target)
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

    config.save_config({"bot_id": "id-atomic"})

    assert len(replacements) == 1
    temporary, destination = replacements[0]
    assert temporary.parent == target.parent
    assert destination == target
    assert fsync_calls
    assert (target.stat().st_mode & 0o777) == 0o600


def test_remember_bot_roundtrip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.delenv("GROKEN_BOT_NAME", raising=False)
    config.remember_bot("id-1", "groken")
    assert config.cached_bot_id() == "id-1"
    assert config.bot_name() == "groken"
    mode = (tmp_path / "config.json").stat().st_mode & 0o777
    assert mode == 0o600
