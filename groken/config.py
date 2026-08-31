import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from typing_extensions import override

from .private_files import write_private_text

CONFIG_DIR = Path.home() / ".config" / "groken"
CONFIG_FILE = CONFIG_DIR / "config.json"
DEFAULT_BOT_NAME = "groken"

LocalStateReason = Literal["malformed JSON", "expected a JSON object"]


# Exception instances must remain mutable so Python can attach traceback state.
@dataclass(slots=True)
class ConfigStateError(Exception):
    path: Path
    reason: LocalStateReason

    @override
    def __str__(self) -> str:
        return (
            f"configuration state is invalid at {self.path} ({self.reason}); "
            "repair or remove it and run: groken configure"
        )


def _object_dict(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    return cast("dict[str, object]", value)


def load_config() -> dict[str, object]:
    if not CONFIG_FILE.exists():
        return {}
    try:
        value = cast("object", json.loads(CONFIG_FILE.read_text()))
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise ConfigStateError(CONFIG_FILE, "malformed JSON") from exc
    config = _object_dict(value)
    if config is None:
        raise ConfigStateError(CONFIG_FILE, "expected a JSON object")
    return config


def save_config(cfg: dict[str, object]) -> None:
    write_private_text(CONFIG_FILE, json.dumps(cfg, indent=2))


def vnc_enabled() -> bool:
    value = load_config().get("vnc")
    vnc = _object_dict(value)
    return vnc is not None and vnc.get("enabled") is True


def set_vnc_enabled(enabled: bool) -> None:
    cfg = load_config()
    cfg["vnc"] = {"enabled": bool(enabled)}
    save_config(cfg)


def bot_name() -> str:
    return os.environ.get("GROKEN_BOT_NAME") or str(
        load_config().get("bot_name") or DEFAULT_BOT_NAME
    )


def cached_bot_id() -> str | None:
    value = load_config().get("bot_id")
    return str(value) if value else None


def remember_bot(bot_id: str, name: str) -> None:
    cfg = load_config()
    cfg["bot_id"] = bot_id
    cfg["bot_name"] = name
    save_config(cfg)
