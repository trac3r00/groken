from groken import config


def test_default_bot_name(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.delenv("GROKEN_BOT_NAME", raising=False)
    assert config.bot_name() == "groken"


def test_env_override_wins(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setenv("GROKEN_BOT_NAME", "custom-bot")
    assert config.bot_name() == "custom-bot"


def test_remember_bot_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.delenv("GROKEN_BOT_NAME", raising=False)
    config.remember_bot("id-1", "groken")
    assert config.cached_bot_id() == "id-1"
    assert config.bot_name() == "groken"
    mode = (tmp_path / "config.json").stat().st_mode & 0o777
    assert mode == 0o600
