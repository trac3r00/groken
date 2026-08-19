import json
import os
from pathlib import Path

CONFIG_DIR = Path.home() / ".config" / "groken"
CONFIG_FILE = CONFIG_DIR / "config.json"
DEFAULT_BOT_NAME = "groken"


def load_config() -> dict[str, object]:
    if CONFIG_FILE.exists():
        return json.loads(CONFIG_FILE.read_text())
    return {}


def save_config(cfg: dict[str, object]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))
    CONFIG_FILE.chmod(0o600)


def bot_name() -> str:
    return os.environ.get("GROKEN_BOT_NAME") or str(load_config().get("bot_name") or DEFAULT_BOT_NAME)


def cached_bot_id() -> str | None:
    value = load_config().get("bot_id")
    return str(value) if value else None


def remember_bot(bot_id: str, name: str) -> None:
    cfg = load_config()
    cfg["bot_id"] = bot_id
    cfg["bot_name"] = name
    save_config(cfg)
