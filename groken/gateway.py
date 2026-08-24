import json
import time
import uuid
from collections.abc import Iterator
from typing import Any

import httpx

from .auth import get_access_token, load_tokens, refresh_tokens
from .checksum import create_cursor_checksum, get_machine_id
from .client import BACKEND_URL, GROK_BOT, ConnectError, detect_client_version
from .config import bot_name, cached_bot_id, remember_bot
from .provisioning import WORKER_DESCRIPTION, WORKER_TITLE

GATEWAY_COMMANDS_TIMEOUT = httpx.Timeout(30.0, read=120.0)


class GatewaySession:
    def __init__(self, gateway_url: str, gateway_token: str, network_token: str, pod_id: str):
        self.gateway_url = gateway_url.rstrip("/")
        self.gateway_token = gateway_token
        self.network_token = network_token
        self.pod_id = pod_id
        self.http = httpx.Client(timeout=GATEWAY_COMMANDS_TIMEOUT)

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

    def agent_busy(self, agent_id: str) -> bool | None:
        try:
            agents = self.command("listAgents")
        except ConnectError:
            return None
        if not isinstance(agents, list):
            return None
        for a in agents:
            if isinstance(a, dict) and a.get("id") == agent_id:
                return bool(a.get("isComposingMessage") or a.get("isRunning"))
        return None

    def ask(
        self,
        agent_id: str,
        text: str,
        timeout_s: float = 600,
        idle_s: float = 45,
        client_nonce: str | None = None,
    ) -> str:
        nonce = client_nonce or str(uuid.uuid4())
        try:
            return self._ask_via_events(agent_id, text, timeout_s, idle_s, nonce)
        except (ConnectError, httpx.HTTPError):
            return self._ask_via_poll(agent_id, text, timeout_s, idle_s, nonce)

    def _ask_via_events(
        self,
        agent_id: str,
        text: str,
        timeout_s: float,
        idle_s: float,
        client_nonce: str,
    ) -> str:
        chunks: list[str] = []
        seen_ids: set[str] = set()
        busy = False
        saw_busy = False
        saw_upsert = False
        deadline = time.monotonic() + timeout_s
        with self.http.stream(
            "GET", f"{self.gateway_url}/events", headers=self._headers(),
        ) as r:
            if r.status_code != 200:
                raise ConnectError(r.status_code, "events stream rejected")
            self.send_prompt(agent_id, text, client_nonce=client_nonce)
            send_epoch_ms = time.time() * 1000
            last_activity = time.monotonic()
            for line in r.iter_lines():
                if time.monotonic() > deadline:
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
                    entry = payload.get("entry") or {}
                    ts = entry.get("timestampMs")
                    if isinstance(ts, (int, float)) and ts < send_epoch_ms - 2000:
                        continue
                    if entry.get("kind") == "send-message":
                        entry_id = entry.get("id")
                        if entry_id is not None and entry_id in seen_ids:
                            continue
                        content = (entry.get("message") or {}).get("content")
                        if isinstance(content, str) and content and content != text:
                            if entry_id is not None:
                                seen_ids.add(entry_id)
                            chunks.append(content)
                            last_activity = time.monotonic()
                elif channel == "agent-upserted":
                    agent = payload.get("agent") or {}
                    if agent.get("id") == agent_id:
                        busy = bool(
                            agent.get("isComposingMessage") or agent.get("isRunning")
                        )
                        saw_busy = saw_busy or busy
                        if not busy and chunks and (saw_busy or saw_upsert):
                            break
                        saw_upsert = True
                if chunks and time.monotonic() - last_activity > idle_s:
                    break
        if not chunks:
            raise ConnectError(0, "events stream ended with no reply content")
        return "\n".join(chunks)

    def _ask_via_poll(
        self,
        agent_id: str,
        text: str,
        timeout_s: float,
        idle_s: float,
        client_nonce: str,
    ) -> str:
        before = {e.get("id") for e in self.transcript_tail(agent_id)}
        self.send_prompt(agent_id, text, client_nonce=client_nonce)
        chunks: list[str] = []
        deadline = time.monotonic() + timeout_s
        last_activity = time.monotonic()
        saw_busy = False
        settle_polls = 0
        while time.monotonic() < deadline:
            time.sleep(2)
            busy = self.agent_busy(agent_id)
            if busy:
                saw_busy = True
            try:
                tail = self.transcript_tail(agent_id)
            except ConnectError:
                continue
            new = [e for e in tail if e.get("id") not in before and e.get("kind") == "send-message"]
            fresh = False
            for e in new:
                content = (e.get("message") or {}).get("content")
                if isinstance(content, str) and content and content != text:
                    marker = e.get("id")
                    if marker not in before:
                        chunks.append(content)
                        before.add(str(marker))
                        fresh = True
            if fresh:
                last_activity = time.monotonic()
                settle_polls = 0
            if saw_busy and busy is False:
                settle_polls += 1
                if settle_polls >= 2:
                    break
            elif chunks and time.monotonic() - last_activity > idle_s:
                break
        return "\n".join(chunks)


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
        agents = self.command("listAgents")
        if cached and any(a.get("id") == cached for a in agents):
            return cached
        name = bot_name()
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
