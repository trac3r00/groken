import json
import time
import uuid
from collections.abc import Callable, Iterator
from typing import Any

import httpx

from .auth import get_access_token, load_tokens, refresh_tokens
from .checksum import create_cursor_checksum, get_machine_id
from .client import BACKEND_URL, GROK_BOT, ConnectError, detect_client_version
from .config import bot_name, cached_bot_id, remember_bot
from .provisioning import WORKER_DESCRIPTION, WORKER_TITLE

GATEWAY_COMMANDS_TIMEOUT = httpx.Timeout(30.0, read=120.0)
_REPLY_QUIET_S = 2.0


class _ReplyCompletion:
    def __init__(self, now: float) -> None:
        self.chunks: list[str] = []
        self.busy: bool | None = None
        self.stable_observations = 0
        self.last_change = now

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
        self.gateway_url = gateway_url.rstrip("/")
        self.gateway_token = gateway_token
        self.network_token = network_token
        self.pod_id = pod_id
        self._monotonic = monotonic or time.monotonic
        self._sleep = sleeper or time.sleep
        self.http = httpx.Client(timeout=GATEWAY_COMMANDS_TIMEOUT)

    def _now(self) -> float:
        return getattr(self, "_monotonic", time.monotonic)()

    def _sleep_for(self, seconds: float) -> None:
        getattr(self, "_sleep", time.sleep)(seconds)

    def _headers(self) -> dict[str, str]:
        return {
            "authorization": f"Bearer {self.gateway_token}",
            "content-type": "application/json",
            "x-anyrun-network-token": self.network_token,
        }

    def command(self, method: str, args: dict[str, Any] | None = None) -> Any:
        r = self.http.post(
            f"{self.gateway_url}/api/{method}",
            headers=self._headers(),
            content=json.dumps(args or {}),
        )
        if r.status_code != 200:
            raise ConnectError(r.status_code, r.text)
        text = r.text.strip()
        return json.loads(text) if text else None

    def events(self, channels: list[str] | None = None) -> Iterator[dict[str, Any]]:
        url = f"{self.gateway_url}/events"
        if channels:
            url += "?channels=" + ",".join(channels)
        with self.http.stream("GET", url, headers=self._headers()) as r:
            if r.status_code != 200:
                raise ConnectError(r.status_code, r.read().decode("utf-8", "replace"))
            event_name = "message"
            for line in r.iter_lines():
                if line.startswith("event:"):
                    event_name = line[6:].strip()
                elif line.startswith("data:"):
                    payload = line[5:].strip()
                    if payload:
                        yield {"event": event_name, "data": json.loads(payload)}
                        event_name = "message"

    def list_agents(self) -> list[dict[str, Any]]:
        return list(self.command("listAgents"))

    def send_prompt(self, agent_id: str, text: str, client_nonce: str | None = None) -> dict[str, Any]:
        return dict(self.command("sendPrompt", {
            "agentId": agent_id,
            "prompt": text,
            "clientNonce": client_nonce or str(uuid.uuid4()),
        }))

    def transcript_tail(self, agent_id: str) -> list[dict[str, Any]]:
        data = self.command("getAgentTranscriptTail", {"id": agent_id})
        return list((data or {}).get("entries", []))

    @staticmethod
    def _authoritative_busy(agent: dict[str, Any]) -> bool | None:
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
        if not isinstance(agents, list):
            return None
        for agent in agents:
            if isinstance(agent, dict) and agent.get("id") == agent_id:
                return self._authoritative_busy(agent)
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
            self.send_prompt(agent_id, text, client_nonce=nonce)

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
        self, agent_id: str, text: str, timeout_s: float = 600,
        idle_s: float = 45, client_nonce: str | None = None,
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
        send_prompt = send_prompt or (
            lambda: self.send_prompt(agent_id, text, client_nonce=client_nonce)
        )
        seen_ids: set[str] = set()
        anchor_id: str | None = None
        deadline = deadline if deadline is not None else self._now() + timeout_s
        send_epoch_ms = time.time() * 1000
        send_prompt()
        attempts = 0

        def accept(entry: dict[str, Any], now: float) -> None:
            nonlocal anchor_id
            if entry.get("kind") != "send-message":
                return
            entry_id = entry.get("id")
            if entry_id is None or entry_id in seen_ids:
                return
            timestamp = entry.get("timestampMs")
            if isinstance(timestamp, (int, float)) and timestamp < send_epoch_ms - 2000:
                return
            content = (entry.get("message") or {}).get("content")
            if not isinstance(content, str) or not content or content == text:
                return
            seen_ids.add(entry_id)
            anchor_id = anchor_id or entry_id
            completion.append(content, now)
            if on_chunk is not None:
                on_chunk(content)

        while attempts <= 6 and self._now() < deadline:
            try:
                with self.http.stream("GET", f"{self.gateway_url}/events", headers=self._headers()) as response:
                    if response.status_code != 200:
                        raise ConnectError(response.status_code, "events stream rejected")
                    for line in response.iter_lines():
                        now = self._now()
                        if now >= deadline:
                            break
                        if not line.startswith("data:"):
                            continue
                        try:
                            frame = json.loads(line[5:].strip())
                        except ValueError:
                            continue
                        channel = frame.get("channel")
                        payload = frame.get("payload") or {}
                        if channel == "transcript" and payload.get("type") == "appended":
                            if payload.get("agentId") not in (None, agent_id):
                                continue
                            accept(payload.get("entry") or {}, now)
                        elif channel == "agent-upserted":
                            agent = payload.get("agent") or {}
                            if agent.get("id") == agent_id:
                                completion.observe(self._authoritative_busy(agent))
                        if completion.complete(self._now()):
                            return "\n".join(completion.chunks)
                        if completion.chunks and self._now() - completion.last_change >= idle_s:
                            raise ConnectError(0, "reply incomplete")
            except (ConnectError, httpx.HTTPError):
                # A stream that never opened is the normal signal to use polling;
                # only a post-anchor disconnect is eligible for transcript resume.
                if attempts == 0 and anchor_id is None:
                    raise
                if attempts >= 6:
                    raise
                tail = self.transcript_tail(agent_id)
                if anchor_id is None or not any(e.get("id") == anchor_id for e in tail):
                    raise ConnectError(0, "transcript anchor lost")
                after_anchor = False
                for entry in tail:
                    if entry.get("id") == anchor_id:
                        after_anchor = True
                        continue
                    if after_anchor:
                        accept(entry, self._now())
                if completion.complete(self._now()):
                    return "\n".join(completion.chunks)
                delay = min(0.25 * (2 ** attempts), 4.0)
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
        send_prompt = send_prompt or (
            lambda: self.send_prompt(agent_id, text, client_nonce=client_nonce)
        )
        deadline = deadline if deadline is not None else self._now() + timeout_s
        if self._now() >= deadline:
            raise ConnectError(0, "reply incomplete")
        if completion.chunks and self._now() - completion.last_change >= idle_s:
            raise ConnectError(0, "reply incomplete")
        before = {e.get("id") for e in self.transcript_tail(agent_id)}
        send_prompt()
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
                content = (entry.get("message") or {}).get("content")
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
        self.access_token = get_access_token()
        self.machine_id = get_machine_id()
        self.client_version = detect_client_version()
        self.http = httpx.Client(timeout=httpx.Timeout(30.0, read=60.0))
        self._session: GatewaySession | None = None

    def _ensure_sandbox(self) -> dict[str, Any]:
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
            return r.json()
        raise ConnectError(401, "unauthorized after refresh")

    def ensure_sandbox_metadata(self) -> dict[str, Any]:
        metadata = self._ensure_sandbox()
        required = {"execDaemonUrl", "networkToken", "execDaemonAuthToken", "podId"}
        missing = required - metadata.keys() if isinstance(metadata, dict) else required
        if missing:
            raise ConnectError(0, f"missing sandbox metadata: {', '.join(sorted(missing))}")
        return metadata

    def session(self, force: bool = False) -> GatewaySession:
        if self._session is None or force:
            box = self._ensure_sandbox()
            self._session = GatewaySession(
                gateway_url=box["gatewayUrl"],
                gateway_token=box["gatewayToken"],
                network_token=box["networkToken"],
                pod_id=box.get("podId", ""),
            )
        return self._session

    @staticmethod
    def _should_remint(error: BaseException) -> bool:
        if isinstance(error, ConnectError):
            return error.status in {0, 401, 403, 404, 408, 429, 502, 503, 504}
        return isinstance(error, (httpx.TransportError, httpx.TimeoutException))

    def command(self, method: str, args: dict[str, Any] | None = None) -> Any:
        try:
            return self.session().command(method, args)
        except (ConnectError, httpx.HTTPError) as error:
            if not self._should_remint(error):
                raise
            return self.session(force=True).command(method, args)

    def send_prompt(
        self,
        agent_id: str,
        text: str,
        client_nonce: str | None = None,
    ) -> dict[str, Any]:
        nonce = client_nonce or str(uuid.uuid4())
        try:
            return self.session().send_prompt(agent_id, text, nonce)
        except (ConnectError, httpx.HTTPError) as error:
            if not self._should_remint(error):
                raise
            return self.session(force=True).send_prompt(agent_id, text, nonce)

    def transcript_tail(self, agent_id: str) -> list[dict[str, Any]]:
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
            return self.session(force=True).ask(agent_id, text, timeout_s, idle_s, nonce)

    def ask_stream(self, agent_id: str, text: str, timeout_s: float = 600,
                   idle_s: float = 45, on_chunk: Callable[[str], None] | None = None) -> str:
        nonce = str(uuid.uuid4())
        try:
            return self.session().ask_stream(agent_id, text, timeout_s, idle_s, nonce, on_chunk)
        except (ConnectError, httpx.HTTPError) as error:
            if not self._should_remint(error):
                raise
            return self.session(force=True).ask_stream(agent_id, text, timeout_s, idle_s, nonce, on_chunk)

    def events(self, channels: list[str] | None = None) -> Iterator[dict[str, Any]]:
        for attempt in range(2):
            try:
                yield from self.session(force=attempt > 0).events(channels)
                return
            except (ConnectError, httpx.HTTPError) as error:
                if attempt == 1 or not self._should_remint(error):
                    raise

    def resolve_agent(self, bot: str | None = None) -> str:
        if bot:
            for a in self.command("listAgents"):
                if a.get("id") == bot or a.get("name") == bot:
                    return str(a["id"])
            raise ValueError(f"unknown bot: {bot}")
        return self.own_agent_id()

    def own_agent_id(self) -> str:
        cached = cached_bot_id()
        name = bot_name()
        agents = self.command("listAgents")
        if cached and any(
            a.get("id") == cached and a.get("name") == name for a in agents
        ):
            return cached
        for a in agents:
            if a.get("name") == name:
                agent_id = str(a["id"])
                remember_bot(agent_id, name)
                if a.get("description") != WORKER_DESCRIPTION:
                    self.command("updateAgent", {"id": agent_id, "description": WORKER_DESCRIPTION})
                return agent_id
        created = self.command("createAgent", {
            "name": name,
            "description": WORKER_DESCRIPTION,
            "title": WORKER_TITLE,
            "clientNonce": str(uuid.uuid4()),
        })
        agent_id = str(created["agent"]["id"])
        remember_bot(agent_id, name)
        return agent_id
