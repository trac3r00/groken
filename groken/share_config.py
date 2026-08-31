"""Validated share-link configuration persistence."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TypeGuard
from urllib.parse import urlparse

from .private_files import write_private_text


class ShareLinkError(ValueError):
    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


@dataclass(frozen=True, slots=True)
class ShareLink:
    """The URL and Bearer token used to reach a shared relay."""

    url: str
    token: str

    def __post_init__(self) -> None:
        parsed = urlparse(self.url)
        if parsed.scheme not in {"http", "https"} or parsed.hostname is None:
            raise ShareLinkError("share URL must be an absolute HTTP or HTTPS URL")
        if parsed.scheme != "https" and parsed.hostname not in {
            "localhost",
            "127.0.0.1",
            "::1",
        }:
            raise ShareLinkError("share URL must use HTTPS except on loopback")
        if not self.token.strip():
            raise ShareLinkError("share token must not be blank")


def save_share_config(link: ShareLink, path: Path) -> None:
    write_private_text(path, json.dumps({"url": link.url, "token": link.token}))


def load_share_config(path: Path) -> ShareLink | None:
    try:
        payload = json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    if not _is_string_mapping(payload):
        return None
    url, token = payload.get("url"), payload.get("token")
    if not isinstance(url, str) or not isinstance(token, str) or not url or not token:
        return None
    try:
        return ShareLink(url, token)
    except ShareLinkError:
        return None


def clear_share_config(path: Path) -> bool:
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True


def _is_string_mapping(value: object) -> TypeGuard[Mapping[str, object]]:
    return isinstance(value, dict) and all(isinstance(key, str) for key in value)
