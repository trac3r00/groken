import json
import time
import uuid
from collections.abc import Callable, Generator, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from queue import Empty, Queue
from threading import Event, Thread
from typing import Protocol, TypeAlias, TypeGuard, cast, final

import httpx

from .auth import get_access_token, load_tokens, refresh_tokens
from .checksum import create_cursor_checksum, get_machine_id
from .client import BACKEND_URL, GROK_BOT, ConnectError, detect_client_version
from .config import bot_name, cached_bot_id, remember_bot
from .provisioning import WORKER_DESCRIPTION, WORKER_TITLE

GATEWAY_COMMANDS_TIMEOUT = httpx.Timeout(30.0, read=120.0)
_REPLY_QUIET_S = 2.0
MAX_BOT_NAME_LENGTH = 256


def _decode_json(value: str | bytes) -> object:
    return cast(object, json.loads(value))


def _is_object_dict(value: object) -> TypeGuard[dict[str, object]]:
    if not isinstance(value, dict):
        return False
    mapping = cast(dict[object, object], value)
    return all(isinstance(key, str) for key in mapping)


def _is_object_list(value: object) -> TypeGuard[list[object]]:
    return isinstance(value, list)


def _require_object_dict(value: object) -> dict[str, object]:
    if not _is_object_dict(value):
        raise TypeError("expected an object")
    return value


def _require_object_dicts(value: object) -> list[dict[str, object]]:
    if not _is_object_list(value):
        raise TypeError("expected an array")
    return [_require_object_dict(item) for item in value]


class _ReplyCompletion:
    def __init__(self, now: float) -> None:
        self.chunks: list[str] = []
        self.busy: bool | None = None
        self.stable_observations: int = 0
        self.last_change: float = now

    def append(self, content: str, now: float) -> None:
        self.chunks.append(content)
        self.stable_observations = 0
        self.last_change = now

    def observe(self, busy: bool | None) -> None:
        self.busy = busy
        if busy is False:
            self.stable_observations += 1
        else:
            self.stable_observations = 0

    def complete(self, now: float) -> bool:
        return bool(
            self.chunks
            and self.busy is False
            and self.stable_observations >= 2
            and now - self.last_change >= _REPLY_QUIET_S
        )


@dataclass(frozen=True, slots=True)
class _FeedFrame:
    frame: dict[str, object]


@dataclass(frozen=True, slots=True)
class _FeedFailure:
    error: Exception


@dataclass(frozen=True, slots=True)
class _FeedEnd:
    pass


_FeedItem: TypeAlias = _FeedFrame | _FeedFailure | _FeedEnd


@final
class GatewayEventFeed:
    """Read SSE frames in one owned thread and expose deadline-aware delivery."""

    def __init__(
        self, response: httpx.Response, frames: Iterator[dict[str, object]]
    ) -> None:
        self._response: httpx.Response = response
        self._frames: Iterator[dict[str, object]] = frames
        self._queue: Queue[_FeedItem] = Queue()
        self._advance = Event()
        self._closed = Event()
        self._thread = Thread(target=self._read, name="groken-event-reader")
        self._thread.start()

    def _read(self) -> None:
        try:
            for frame in self._frames:
                if self._closed.is_set():
                    break
                self._queue.put(_FeedFrame(frame))
                _ = self._advance.wait()
                self._advance.clear()
                if self._closed.is_set():
                    break
        except Exception as exc:  # noqa: BLE001 - exact reader failure crosses the thread boundary.
            self._queue.put(_FeedFailure(exc))
        finally:
            self._queue.put(_FeedEnd())

    def __iter__(self) -> "GatewayEventFeed":
        return self

    def __next__(self) -> dict[str, object]:
        return self.next_event(None)

    def next_event(
        self, timeout_s: float | None, *, hold: bool = False
    ) -> dict[str, object]:
        try:
            item = self._queue.get(timeout=timeout_s)
        except Empty as exc:
            raise TimeoutError from exc
        match item:
            case _FeedFrame(frame=frame):
                if not hold:
                    self._advance.set()
                return frame
            case _FeedFailure(error=error):
                raise error
            case _FeedEnd():
                raise StopIteration

    def resume(self) -> None:
        self._advance.set()

    def close(self) -> None:
        self._closed.set()
        self._advance.set()
        self._response.close()
        self._thread.join()


class BotUpdateError(Exception):
    """Base class for update gateway failures."""


@final
class UpdateIndeterminateError(BotUpdateError):
    def __init__(self, bot_id: str) -> None:
        self.bot_id: str = bot_id
        detail = (
            f"update outcome for {bot_id} is indeterminate after a transport failure; "
        )
        super().__init__(detail + "inspect Bot status before retrying")


@final
class UpdateReadinessError(BotUpdateError):
    def __init__(self, detail: str) -> None:
        self.detail: str = detail
        super().__init__(detail)


@dataclass(frozen=True, slots=True)
class UpdateAvailability:
    host: bool | None
    image: bool | None


@dataclass(frozen=True, slots=True)
class UpdateBoxState:
    state: str | None
    vnc_url: str | None

    @property
    def ready(self) -> bool:
        return self.state == "running" and bool(self.vnc_url)


def update_box_state(frame: dict[str, object], bot_id: str) -> UpdateBoxState | None:
    data = frame.get("data")
    envelope = data if _is_object_dict(data) else {}
    payload_value = envelope.get("payload")
    payload = payload_value if _is_object_dict(payload_value) else {}
    if envelope.get("channel") != "forever-box" or payload.get("agentId") != bot_id:
        return None
    state_value, vnc_value = payload.get("state"), payload.get("vncUrl")
    return UpdateBoxState(
        state_value if isinstance(state_value, str) else None,
        vnc_value if isinstance(vnc_value, str) else None,
    )


class UpdateEventFeed(Protocol):
    def next_event(
        self, timeout_s: float | None, *, hold: bool = False
    ) -> dict[str, object]: ...
    def resume(self) -> None: ...


class UpdateGateway(Protocol):
    def resolve_agent(self, bot: str | None = None) -> str: ...
    def command(self, method: str, args: dict[str, object] | None = None) -> object: ...
    def command_once(
        self, method: str, args: dict[str, object] | None = None
    ) -> object: ...
    def event_subscription(
        self, channels: list[str], timeout_s: float | None
    ) -> AbstractContextManager[UpdateEventFeed]: ...
    def ask(self, agent_id: str, text: str, timeout_s: float = 600) -> str: ...


class GatewaySession:
    def __init__(
        self,
        gateway_url: str,
        gateway_token: str,
        network_token: str,
        pod_id: str,
        *,
        monotonic: Callable[[], float] | None = None,
        sleeper: Callable[[float], None] | None = None,
    ):
        self.gateway_url: str = gateway_url.rstrip("/")
        self.gateway_token: str = gateway_token
        self.network_token: str = network_token
        self.pod_id: str = pod_id
        self._monotonic: Callable[[], float] = monotonic or time.monotonic
        self._sleep: Callable[[float], None] = sleeper or time.sleep
        self.http: httpx.Client = httpx.Client(timeout=GATEWAY_COMMANDS_TIMEOUT)

    def close(self) -> None:
        self.http.close()

    def _now(self) -> float:
        clock: Callable[[], float] = getattr(self, "_monotonic", time.monotonic)
        return clock()

    def _sleep_for(self, seconds: float) -> None:
        sleeper: Callable[[float], None] = getattr(self, "_sleep", time.sleep)
        sleeper(seconds)

    def _headers(self) -> dict[str, str]:
        return {
            "authorization": f"Bearer {self.gateway_token}",
            "content-type": "application/json",
            "x-anyrun-network-token": self.network_token,
        }

    def command(self, method: str, args: dict[str, object] | None = None) -> object:
        r = self.http.post(
            f"{self.gateway_url}/api/{method}",
            headers=self._headers(),
            content=json.dumps(args or {}),
        )
        if r.status_code != 200:
            raise ConnectError(r.status_code, r.text)
        text = r.text.strip()
        if not text:
            return None
        decoded = _decode_json(text)
        return decoded

    @staticmethod
    def _event_frames(lines: Iterator[str]) -> Iterator[dict[str, object]]:
        event_name = "message"
        for line in lines:
            if line.startswith("event:"):
                event_name = line[6:].strip()
            elif line.startswith("data:"):
                payload = line[5:].strip()
                if payload:
                    decoded = _decode_json(payload)
                    yield {"event": event_name, "data": decoded}
                    event_name = "message"

    @contextmanager
    def event_subscription(
        self, channels: list[str], timeout_s: float | None
    ) -> Generator[GatewayEventFeed, None, None]:
        """Open and validate an SSE subscription before yielding its owned feed."""
        url = f"{self.gateway_url}/events"
        if channels:
            url += "?channels=" + ",".join(channels)
        with self.http.stream(
            "GET",
            url,
            headers=self._headers(),
            timeout=httpx.Timeout(timeout_s),
        ) as response:
            if response.status_code != 200:
                raise ConnectError(
                    response.status_code,
                    response.read().decode("utf-8", "replace"),
                )
            feed = GatewayEventFeed(response, self._event_frames(response.iter_lines()))
            try:
                yield feed
            finally:
                feed.close()

    def events(self, channels: list[str] | None = None) -> Iterator[dict[str, object]]:
        with self.event_subscription(channels or [], 120.0) as frames:
            yield from frames

    def list_agents(self) -> list[dict[str, object]]:
        return _require_object_dicts(self.command("listAgents"))

    def send_prompt(
        self, agent_id: str, text: str, client_nonce: str | None = None
    ) -> dict[str, object]:
        return _require_object_dict(
            self.command(
                "sendPrompt",
                {
                    "agentId": agent_id,
                    "prompt": text,
                    "clientNonce": client_nonce or str(uuid.uuid4()),
                },
            )
        )

    def transcript_tail(self, agent_id: str) -> list[dict[str, object]]:
        data = self.command(
            "getAgentTranscriptTail",
            {"id": agent_id, "limit": 100},
        )
        if data is None:
            return []
        entries = _require_object_dict(data).get("entries", [])
        return _require_object_dicts(entries)

    @staticmethod
    def _authoritative_busy(agent: dict[str, object]) -> bool | None:
        composing = agent.get("isComposingMessage")
        running = agent.get("isRunning")
        if composing is True or running is True:
            return True
        if composing is False and running is False:
            return False
        return None

    def agent_busy(self, agent_id: str) -> bool | None:
        try:
            agents = self.command("listAgents")
        except ConnectError:
            return None
        if not _is_object_list(agents):
            return None
        for value in agents:
            if _is_object_dict(value) and value.get("id") == agent_id:
                return self._authoritative_busy(value)
        return None

    def ask(
        self,
        agent_id: str,
        text: str,
        timeout_s: float = 600,
        idle_s: float = 45,
        client_nonce: str | None = None,
        on_chunk: Callable[[str], None] | None = None,
    ) -> str:
        nonce = client_nonce or str(uuid.uuid4())
        completion = _ReplyCompletion(self._now())
        deadline = self._now() + timeout_s
        prompt_sent = False

        def send_prompt_once() -> None:
            nonlocal prompt_sent
            if prompt_sent:
                return
            prompt_sent = True
            _ = self.send_prompt(agent_id, text, client_nonce=nonce)

        try:
            return self._ask_via_events(
                agent_id,
                text,
                timeout_s,
                idle_s,
                nonce,
                completion,
                send_prompt_once,
                deadline,
                on_chunk,
            )
        except (ConnectError, httpx.HTTPError):
            return self._ask_via_poll(
                agent_id,
                text,
                timeout_s,
                idle_s,
                nonce,
                completion,
                send_prompt_once,
                deadline,
                on_chunk,
            )

    def ask_stream(
        self,
        agent_id: str,
        text: str,
        timeout_s: float = 600,
        idle_s: float = 45,
        client_nonce: str | None = None,
        on_chunk: Callable[[str], None] | None = None,
    ) -> str:
        return self.ask(agent_id, text, timeout_s, idle_s, client_nonce, on_chunk)

    def _ask_via_events(
        self,
        agent_id: str,
        text: str,
        timeout_s: float,
        idle_s: float,
        client_nonce: str,
        completion: _ReplyCompletion | None = None,
        send_prompt: Callable[[], None] | None = None,
        deadline: float | None = None,
        on_chunk: Callable[[str], None] | None = None,
    ) -> str:
        completion = completion or _ReplyCompletion(self._now())
        prompt_sender = send_prompt
        if prompt_sender is None:

            def default_send_prompt() -> None:
                _ = self.send_prompt(agent_id, text, client_nonce=client_nonce)

            prompt_sender = default_send_prompt
        seen_ids: set[object] = set()
        anchor_ids: list[object] = []
        deadline = deadline if deadline is not None else self._now() + timeout_s
        send_epoch_ms = time.time() * 1000
        attempts: int = 0

        def accept(entry: dict[str, object], now: float) -> None:
            if entry.get("kind") != "send-message":
                return
            entry_id = entry.get("id")
            if entry_id is None or entry_id in seen_ids:
                return
            timestamp = entry.get("timestampMs")
            if isinstance(timestamp, (int, float)) and timestamp < send_epoch_ms - 2000:
                return
            message = entry.get("message")
            content = message.get("content") if _is_object_dict(message) else None
            if not isinstance(content, str) or not content or content == text:
                return
            seen_ids.add(entry_id)
            if not anchor_ids:
                anchor_ids.append(entry_id)
            completion.append(content, now)
            if on_chunk is not None:
                on_chunk(content)

        while attempts <= 6 and self._now() < deadline:
            try:
                with self.http.stream(
                    "GET",
                    f"{self.gateway_url}/events",
                    headers=self._headers(),
                    timeout=httpx.Timeout(30.0, read=_REPLY_QUIET_S),
                ) as response:
                    if response.status_code != 200:
                        raise ConnectError(
                            response.status_code, "events stream rejected"
                        )
                    prompt_sender()
                    for line in response.iter_lines():
                        now = self._now()
                        if now >= deadline:
                            break
                        if not line.startswith("data:"):
                            continue
                        try:
                            decoded = _decode_json(line[5:].strip())
                        except ValueError:
                            continue
                        frame = _require_object_dict(decoded)
                        channel = frame.get("channel")
                        payload = _require_object_dict(frame.get("payload") or {})
                        if (
                            channel == "transcript"
                            and payload.get("type") == "appended"
                        ):
                            if payload.get("agentId") not in (None, agent_id):
                                continue
                            accept(
                                _require_object_dict(payload.get("entry") or {}), now
                            )
                        elif channel == "agent-upserted":
                            agent = _require_object_dict(payload.get("agent") or {})
                            if agent.get("id") == agent_id:
                                completion.observe(self._authoritative_busy(agent))
                        if completion.complete(self._now()):
                            return "\n".join(completion.chunks)
                        if (
                            completion.chunks
                            and self._now() - completion.last_change >= idle_s
                        ):
                            raise ConnectError(0, "reply incomplete")
            except (ConnectError, httpx.HTTPError):
                # A stream that never opened is the normal signal to use polling;
                # only a post-anchor disconnect is eligible for transcript resume.
                if attempts == 0 and not anchor_ids:
                    raise
                if attempts >= 6:
                    raise
                tail = self.transcript_tail(agent_id)
                if not anchor_ids or not any(
                    e.get("id") == anchor_ids[0] for e in tail
                ):
                    raise ConnectError(0, "transcript anchor lost")
                after_anchor = False
                for entry in tail:
                    if entry.get("id") == anchor_ids[0]:
                        after_anchor = True
                        continue
                    if after_anchor:
                        accept(entry, self._now())
                completion.observe(self.agent_busy(agent_id))
                if completion.complete(self._now()):
                    return "\n".join(completion.chunks)
                delay = min(0.25 * (2.0**attempts), 4.0)
                if self._now() + delay >= deadline:
                    break
                self._sleep_for(delay)
                attempts += 1
                continue
            break
        raise ConnectError(0, "reply incomplete")

    def _ask_via_poll(
        self,
        agent_id: str,
        text: str,
        timeout_s: float,
        idle_s: float,
        client_nonce: str,
        completion: _ReplyCompletion | None = None,
        send_prompt: Callable[[], None] | None = None,
        deadline: float | None = None,
        on_chunk: Callable[[str], None] | None = None,
    ) -> str:
        completion = completion or _ReplyCompletion(self._now())
        prompt_sender = send_prompt
        if prompt_sender is None:

            def default_send_prompt() -> None:
                _ = self.send_prompt(agent_id, text, client_nonce=client_nonce)

            prompt_sender = default_send_prompt
        deadline = deadline if deadline is not None else self._now() + timeout_s
        if self._now() >= deadline:
            raise ConnectError(0, "reply incomplete")
        if completion.chunks and self._now() - completion.last_change >= idle_s:
            raise ConnectError(0, "reply incomplete")
        before = {e.get("id") for e in self.transcript_tail(agent_id)}
        prompt_sender()
        while self._now() < deadline:
            self._sleep_for(2)
            now = self._now()
            if now >= deadline:
                break
            busy = self.agent_busy(agent_id)
            try:
                tail = self.transcript_tail(agent_id)
            except ConnectError:
                completion.observe(None)
                continue
            for entry in tail:
                marker = entry.get("id")
                if marker in before or entry.get("kind") != "send-message":
                    continue
                message = entry.get("message")
                content = message.get("content") if _is_object_dict(message) else None
                if isinstance(content, str) and content and content != text:
                    completion.append(content, now)
                    if on_chunk is not None:
                        on_chunk(content)
                before.add(marker)
            completion.observe(busy)
            now = self._now()
            if completion.complete(now):
                return "\n".join(completion.chunks)
            if completion.chunks and now - completion.last_change >= idle_s:
                break
        raise ConnectError(0, "reply incomplete")


class GatewayManager:
    def __init__(self):
        self.access_token: str = get_access_token()
        self.machine_id: str = get_machine_id()
        self.client_version: str = detect_client_version()
        self.http: httpx.Client = httpx.Client(timeout=httpx.Timeout(30.0, read=60.0))
        self._session: GatewaySession | None = None

    def close(self) -> None:
        session = self._session
        self._session = None
        if session is not None:
            session.close()
        self.http.close()

    def _ensure_sandbox(self) -> dict[str, object]:
        for attempt in range(2):
            r = self.http.post(
                f"{BACKEND_URL}/{GROK_BOT}/EnsureSandBox",
                headers={
                    "authorization": f"Bearer {self.access_token}",
                    "x-cursor-checksum": create_cursor_checksum(self.machine_id),
                    "x-cursor-client-type": "sand",
                    "x-cursor-client-version": self.client_version,
                    "x-sand-box-namespace": "prod",
                    "x-ghost-mode": "false",
                    "x-request-id": str(uuid.uuid4()),
                    "connect-protocol-version": "1",
                    "content-type": "application/json",
                },
                json={},
            )
            if r.status_code == 401 and attempt == 0:
                tokens = load_tokens() or {}
                fresh = refresh_tokens(str(tokens.get("refreshToken", "")))
                if fresh and "accessToken" in fresh:
                    self.access_token = str(fresh["accessToken"])
                    continue
            if r.status_code != 200:
                message = (
                    "unauthorized after refresh"
                    if r.status_code == 401 and attempt == 1
                    else r.text
                )
                raise ConnectError(r.status_code, message)
            decoded = cast(object, r.json())
            return _require_object_dict(decoded)
        raise ConnectError(401, "unauthorized after refresh")

    def ensure_sandbox_metadata(self) -> dict[str, object]:
        metadata = self._ensure_sandbox()
        required = {"execDaemonUrl", "networkToken", "execDaemonAuthToken", "podId"}
        missing = required - metadata.keys()
        if missing:
            raise ConnectError(
                0, f"missing sandbox metadata: {', '.join(sorted(missing))}"
            )
        return metadata

    def session(self, force: bool = False) -> GatewaySession:
        if self._session is None or force:
            box = self._ensure_sandbox()
            replacement = GatewaySession(
                gateway_url=str(box["gatewayUrl"]),
                gateway_token=str(box["gatewayToken"]),
                network_token=str(box["networkToken"]),
                pod_id=str(box.get("podId", "")),
            )
            previous = self._session
            self._session = replacement
            if previous is not None:
                previous.close()
        return self._session

    @staticmethod
    def _should_remint(error: BaseException) -> bool:
        if isinstance(error, ConnectError):
            return error.status in {0, 401, 403, 404, 408, 429, 502, 503, 504}
        return isinstance(error, (httpx.TransportError, httpx.TimeoutException))

    def command(self, method: str, args: dict[str, object] | None = None) -> object:
        if method == "duplicateAgent":
            return self.session().command(method, args)
        try:
            return self.session().command(method, args)
        except (ConnectError, httpx.HTTPError) as error:
            if not self._should_remint(error):
                raise
            return self.session(force=True).command(method, args)

    def command_once(
        self, method: str, args: dict[str, object] | None = None
    ) -> object:
        """Issue a non-idempotent command once without transport remint retries."""
        return self.session().command(method, args)

    def event_subscription(
        self, channels: list[str], timeout_s: float | None
    ) -> AbstractContextManager[GatewayEventFeed]:
        return self.session().event_subscription(channels, timeout_s)

    def send_prompt(
        self,
        agent_id: str,
        text: str,
        client_nonce: str | None = None,
    ) -> dict[str, object]:
        nonce = client_nonce or str(uuid.uuid4())
        try:
            return self.session().send_prompt(agent_id, text, nonce)
        except (ConnectError, httpx.HTTPError) as error:
            if not self._should_remint(error):
                raise
            return self.session(force=True).send_prompt(agent_id, text, nonce)

    def transcript_tail(self, agent_id: str) -> list[dict[str, object]]:
        try:
            return self.session().transcript_tail(agent_id)
        except (ConnectError, httpx.HTTPError) as error:
            if not self._should_remint(error):
                raise
            return self.session(force=True).transcript_tail(agent_id)

    def ask(
        self,
        agent_id: str,
        text: str,
        timeout_s: float = 600,
        idle_s: float = 45,
    ) -> str:
        nonce = str(uuid.uuid4())
        try:
            return self.session().ask(agent_id, text, timeout_s, idle_s, nonce)
        except (ConnectError, httpx.HTTPError) as error:
            if not self._should_remint(error):
                raise
            return self.session(force=True).ask(
                agent_id, text, timeout_s, idle_s, nonce
            )

    def ask_stream(
        self,
        agent_id: str,
        text: str,
        timeout_s: float = 600,
        idle_s: float = 45,
        on_chunk: Callable[[str], None] | None = None,
    ) -> str:
        nonce = str(uuid.uuid4())
        try:
            return self.session().ask_stream(
                agent_id, text, timeout_s, idle_s, nonce, on_chunk
            )
        except (ConnectError, httpx.HTTPError) as error:
            if not self._should_remint(error):
                raise
            return self.session(force=True).ask_stream(
                agent_id, text, timeout_s, idle_s, nonce, on_chunk
            )

    def events(self, channels: list[str] | None = None) -> Iterator[dict[str, object]]:
        for attempt in range(2):
            try:
                yield from self.session(force=attempt > 0).events(channels)
                return
            except (ConnectError, httpx.HTTPError) as error:
                if attempt == 1 or not self._should_remint(error):
                    raise

    def _provision_bot(
        self, agent: dict[str, object], name: str | None = None
    ) -> dict[str, object]:
        agent_id = str(agent["id"])
        existing_name = agent.get("name")
        selected_name = name if name is not None else existing_name
        if not isinstance(selected_name, str) or not selected_name:
            raise ConnectError(500, f"agent {agent_id} response is missing name")
        if (
            agent.get("description") != WORKER_DESCRIPTION
            or existing_name != selected_name
        ):
            profile = {
                "name": selected_name,
                "description": WORKER_DESCRIPTION,
            }
            _ = self.command("updateAgent", {"id": agent_id, "profile": profile})
        return {
            **agent,
            "name": selected_name,
            "description": WORKER_DESCRIPTION,
        }

    @staticmethod
    def _normalized_bot_name(name: str, label: str = "bot name") -> str:
        normalized = name.strip()
        if not normalized:
            raise ConnectError(400, f"{label} must not be empty")
        if len(normalized) > MAX_BOT_NAME_LENGTH:
            raise ConnectError(
                400, f"{label} must be at most {MAX_BOT_NAME_LENGTH} characters"
            )
        return normalized

    @staticmethod
    def _normalized_source_selector(source_name: str) -> str:
        source = source_name.strip()
        if not source:
            raise ConnectError(400, "source bot must not be empty")
        return source

    def create_bot(self, name: str) -> dict[str, object]:
        normalized = self._normalized_bot_name(name)
        created = _require_object_dict(
            self.command(
                "createAgent",
                {
                    "name": normalized,
                    "description": WORKER_DESCRIPTION,
                    "title": WORKER_TITLE,
                    "clientNonce": str(uuid.uuid4()),
                },
            )
        )
        return self._provision_bot(_require_object_dict(created["agent"]), normalized)

    def duplicate_bot(self, source_name: str, name: str) -> dict[str, object]:
        source = self._normalized_source_selector(source_name)
        normalized = self._normalized_bot_name(name)
        source_agent = next(
            (
                agent
                for agent in _require_object_dicts(self.command("listAgents"))
                if agent.get("id") == source or agent.get("name") == source
            ),
            None,
        )
        if source_agent is None:
            raise ConnectError(404, f"unknown source bot: {source}")
        duplicate_args: dict[str, object] = {"id": str(source_agent["id"])}
        try:
            try:
                duplicated_value = self.command("duplicateAgent", duplicate_args)
            except ConnectError as error:
                if error.status not in {401, 403}:
                    raise
                duplicated_value = self.session(force=True).command(
                    "duplicateAgent", duplicate_args
                )
        except httpx.HTTPError as error:
            raise ConnectError(
                500,
                "duplicate outcome indeterminate after a transport failure; inspect the Bot roster before retrying",
            ) from error
        duplicated = _require_object_dict(duplicated_value)
        return self._provision_bot(
            _require_object_dict(duplicated["agent"]), normalized
        )

    def resolve_agent(self, bot: str | None = None) -> str:
        if bot:
            for a in _require_object_dicts(self.command("listAgents")):
                if a.get("id") == bot or a.get("name") == bot:
                    return str(a["id"])
            raise ValueError(f"unknown bot: {bot}")
        return self.own_agent_id()

    def own_agent_id(self) -> str:
        cached = cached_bot_id()
        name = bot_name()
        agents = _require_object_dicts(self.command("listAgents"))
        if cached and any(
            a.get("id") == cached and a.get("name") == name for a in agents
        ):
            return cached
        for a in agents:
            if a.get("name") == name:
                agent_id = str(a["id"])
                remember_bot(agent_id, name)
                if a.get("description") != WORKER_DESCRIPTION:
                    _ = self.command(
                        "updateAgent",
                        {
                            "id": agent_id,
                            "profile": {
                                "name": name,
                                "description": WORKER_DESCRIPTION,
                            },
                        },
                    )
                return agent_id
        created = self.create_bot(name)
        agent_id = str(created["id"])
        remember_bot(agent_id, name)
        return agent_id
