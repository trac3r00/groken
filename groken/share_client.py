from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Literal, TypeGuard, overload

import httpx

from .exec_service import ExecResult
from .share_config import (
    ShareLink,
    ShareLinkError,
    clear_share_config,
    load_share_config,
    save_share_config,
)
from .share_protocol import (
    EventFeed,
    SharePermissionError,
    ShareProtocolError,
    ShareRemoteError,
)

__all__ = [
    "RelayManager",
    "ShareLink",
    "ShareLinkError",
    "SharePermissionError",
    "ShareProtocolError",
    "ShareRemoteError",
    "clear_share_link",
    "load_share_link",
    "save_share_link",
]

_DEFAULT_PATH = Path.home() / ".config" / "groken" / "share.json"


def save_share_link(link: ShareLink, path: Path | None = None) -> None:
    """Persist a share link in a private configuration file."""
    save_share_config(link, path or _DEFAULT_PATH)


def load_share_link(path: Path | None = None) -> ShareLink | None:
    """Load a share link, returning no link when the file is absent."""
    return load_share_config(path or _DEFAULT_PATH)


def clear_share_link(path: Path | None = None) -> bool:
    """Remove the saved share link if it exists."""
    return clear_share_config(path or _DEFAULT_PATH)


def _is_string_mapping(value: object) -> TypeGuard[Mapping[str, object]]:
    return isinstance(value, dict) and all(isinstance(key, str) for key in value)


class RelayManager:
    """_GatewayManager-compatible client for a share relay."""

    def __init__(self, link: ShareLink, *, http: httpx.Client | None = None) -> None:
        self.link: ShareLink = link
        self.http: httpx.Client = http or httpx.Client(
            timeout=httpx.Timeout(30.0, read=60.0)
        )
        self._owns_http: bool = http is None
        self._closed: bool = False

    def close(self) -> None:
        """Close the internally created HTTP client at most once."""
        if self._closed:
            return
        self._closed = True
        if self._owns_http:
            self.http.close()

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_data: dict[str, object] | None = None,
        timeout: httpx.Timeout | None = None,
    ) -> httpx.Response:
        url = self.link.url.rstrip("/") + path
        headers = {"authorization": f"Bearer {self.link.token}"}
        try:
            if timeout is None:
                response = (
                    self.http.request(method, url, headers=headers)
                    if json_data is None
                    else self.http.request(method, url, headers=headers, json=json_data)
                )
            else:
                response = (
                    self.http.request(method, url, headers=headers, timeout=timeout)
                    if json_data is None
                    else self.http.request(
                        method,
                        url,
                        headers=headers,
                        json=json_data,
                        timeout=timeout,
                    )
                )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise ShareRemoteError("share relay request failed") from exc
        return response

    @overload
    def command(
        self, method: Literal["listAgents"], args: None = None
    ) -> list[dict[str, object]]: ...

    @overload
    def command(
        self,
        method: Literal["getForeverBoxStatus", "ensureForeverBox"],
        args: dict[str, object],
    ) -> dict[str, object]: ...

    @overload
    def command(self, method: str, args: dict[str, object] | None = None) -> object: ...

    def command(self, method: str, args: dict[str, object] | None = None) -> object:
        if method in {"createAgent", "duplicateAgent"}:
            raise SharePermissionError("bot creation is not available through a share")
        return self._request(
            "POST",
            "/v1/command",
            json_data={"method": method, "args": args or {}},
        ).json()

    def command_once(
        self, method: str, args: dict[str, object] | None = None
    ) -> object:
        return self.command(method, args)

    def create_bot(self, name: str) -> dict[str, object]:
        del name
        raise SharePermissionError("bot creation is not available through a share")

    def duplicate_bot(self, source_name: str, name: str) -> dict[str, object]:
        del source_name, name
        raise SharePermissionError("bot duplication is not available through a share")

    def resolve_agent(self, bot: str | None = None) -> str:
        value = self._request("GET", "/v1/bot").json()
        if not _is_string_mapping(value):
            raise ValueError("relay returned invalid bot metadata")
        agent_id = value.get("agent_id")
        name = value.get("name")
        if bot is not None and bot not in {agent_id, name}:
            raise SharePermissionError(
                f"bot is unavailable through this share: {bot}"
            )
        if not isinstance(agent_id, str):
            raise TypeError("relay returned invalid bot metadata")
        return agent_id

    def own_agent_id(self) -> str:
        return self.resolve_agent()

    def send_prompt(self, agent_id: str, text: str) -> dict[str, object]:
        del agent_id
        value = self._request("POST", "/v1/send", json_data={"text": text}).json()
        if not _is_string_mapping(value):
            raise ValueError("relay returned invalid send response")
        return dict(value)

    def transcript_tail(self, agent_id: str) -> list[dict[str, object]]:
        del agent_id
        value = self._request("GET", "/v1/transcript").json()
        if not isinstance(value, list) or not all(
            _is_string_mapping(item) for item in value
        ):
            raise ValueError("relay returned invalid transcript")
        return [dict(item) for item in value]

    def ask(self, agent_id: str, text: str, timeout_s: float = 600) -> str:
        del agent_id
        value = self._request(
            "POST",
            "/v1/ask",
            json_data={"text": text, "timeout_s": timeout_s},
            timeout=httpx.Timeout(30.0, read=timeout_s + 30.0),
        ).json()
        if not _is_string_mapping(value):
            raise ValueError("relay returned invalid reply")
        reply = value.get("reply")
        if not isinstance(reply, str):
            raise TypeError("relay returned invalid reply")
        return reply

    def ask_stream(
        self,
        agent_id: str,
        text: str,
        timeout_s: float = 600,
        idle_s: float = 45,
        on_chunk: Callable[[str], None] | None = None,
    ) -> str:
        del agent_id
        read_timeout = max(30.0, idle_s + 15.0)
        timeout = httpx.Timeout(30.0, read=read_timeout)
        try:
            with self.http.stream(
                "POST",
                self.link.url.rstrip("/") + "/v1/ask/stream",
                headers={"authorization": f"Bearer {self.link.token}"},
                json={"text": text, "timeout_s": timeout_s, "idle_s": idle_s},
                timeout=timeout,
            ) as response:
                response.raise_for_status()
                for frame in EventFeed(response.iter_lines()):
                    event = frame.get("event")
                    data = frame.get("data")
                    if event == "chunk" and _is_string_mapping(data):
                        chunk = data.get("text")
                        if isinstance(chunk, str) and on_chunk is not None:
                            on_chunk(chunk)
                        continue
                    if event == "done" and _is_string_mapping(data):
                        reply = data.get("reply")
                        if isinstance(reply, str):
                            return reply
                        raise ShareProtocolError("relay done frame has no reply")
                    if event == "error" and _is_string_mapping(data):
                        detail = data.get("detail")
                        raise ShareRemoteError(
                            detail if isinstance(detail, str) else "relay stream failed"
                        )
        except httpx.HTTPError as exc:
            raise ShareRemoteError("share relay stream failed") from exc
        raise ShareProtocolError("relay stream ended before done")

    def execute(
        self,
        command: str,
        working_directory: str = "/workspace",
        timeout_ms: int = 15000,
    ) -> ExecResult:
        value = self._request(
            "POST",
            "/v1/exec",
            json_data={
                "command": command,
                "cwd": working_directory,
                "timeout_ms": timeout_ms,
            },
            timeout=httpx.Timeout(30.0, read=max(30.0, timeout_ms / 1000.0 + 30.0)),
        ).json()
        if not _is_string_mapping(value):
            raise ShareProtocolError("relay returned invalid exec result")
        stdout = value.get("stdout")
        stderr = value.get("stderr")
        exit_code = value.get("exit_code")
        if (
            not isinstance(stdout, str)
            or not isinstance(stderr, str)
            or not isinstance(exit_code, int)
            or isinstance(exit_code, bool)
        ):
            raise ShareProtocolError("relay returned invalid exec result")
        return ExecResult(stdout, stderr, exit_code)

    def vnc_url(self) -> str:
        value = self._request("POST", "/v1/vnc", json_data={}).json()
        if not _is_string_mapping(value):
            raise ShareProtocolError("relay returned invalid VNC result")
        url = value.get("url")
        if not isinstance(url, str):
            raise ShareProtocolError("relay returned invalid VNC result")
        return url

    @contextmanager
    def event_subscription(
        self, channels: list[str], timeout_s: float | None
    ) -> Iterator[EventFeed]:
        del timeout_s
        params = {"channels": ",".join(channels)} if channels else None
        try:
            with self.http.stream(
                "GET",
                self.link.url.rstrip("/") + "/v1/events",
                headers={"authorization": f"Bearer {self.link.token}"},
                params=params,
                timeout=httpx.Timeout(30.0, read=None),
            ) as response:
                response.raise_for_status()
                yield EventFeed(response.iter_lines())
        except httpx.HTTPError as exc:
            raise ShareRemoteError("share relay stream failed") from exc

    def events(self, channels: list[str] | None = None) -> Iterator[dict[str, object]]:
        with self.event_subscription(channels or [], 120.0) as feed:
            while True:
                try:
                    yield feed.next_event(120.0)
                except StopIteration:
                    return

    def ensure_sandbox_metadata(self) -> dict[str, object]:
        raise SharePermissionError("sandbox credentials are not exposed by shares")
