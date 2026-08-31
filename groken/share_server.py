"""FastAPI relay surface for one immutable, revocable Bot share grant."""

# allow: SIZE_OK - cohesive route assembly keeps one auditable auth/revalidation policy.

import json
from collections.abc import AsyncIterator, Callable, Iterator
from functools import partial
from typing import Annotated, TypeGuard

import httpx
from anyio import (
    EndOfStream,
    create_memory_object_stream,
    create_task_group,
    fail_after,
)
from anyio.from_thread import run as run_from_thread
from anyio.to_thread import run_sync
from fastapi import Depends, FastAPI, Header, HTTPException, status
from fastapi.responses import StreamingResponse

from .client import ConnectError
from .exec_service import ExecServiceClient
from .share_server_contracts import (
    AskRequest,
    CommandRequest,
    ExecRequest,
    ExecRunner,
    ShareAuthenticator,
    ShareEventFeed,
    ShareManager,
    StreamItem,
    TextRequest,
    VncRequest,
)
from .share_server_contracts import (
    ShareContext as _ShareContext,
)
from .share_server_contracts import (
    StreamChunk as _StreamChunk,
)
from .share_server_contracts import (
    StreamDone as _StreamDone,
)
from .share_server_contracts import (
    StreamError as _StreamError,
)
from .vnc import display_from_forever_box, vnc_url

__all__ = ["ShareManager", "create_share_app"]

_ALLOWED_COMMANDS = frozenset({"listAgents"})


def _is_object_dict(value: object) -> TypeGuard[dict[str, object]]:
    return isinstance(value, dict) and all(isinstance(key, str) for key in value)


def _project_send(value: object) -> dict[str, bool]:
    if not _is_object_dict(value):
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "malformed gateway response")
    accepted = value.get("accepted")
    if not isinstance(accepted, bool):
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "malformed gateway response")
    return {"accepted": accepted}


def _project_transcript(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list) or not all(_is_object_dict(item) for item in value):
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "malformed gateway response")
    projected: list[dict[str, object]] = []
    allowed_types: dict[str, type[object] | tuple[type[object], ...]] = {
        "id": str,
        "kind": str,
        "timestampMs": (int, float),
        "content": str,
    }
    for item in value:
        entry: dict[str, object] = {}
        for key, expected in allowed_types.items():
            field = item.get(key)
            if field is not None:
                if not isinstance(field, expected):
                    raise HTTPException(
                        status.HTTP_502_BAD_GATEWAY, "malformed gateway response"
                    )
                entry[key] = field
        message = item.get("message")
        if message is not None:
            if not _is_object_dict(message):
                raise HTTPException(
                    status.HTTP_502_BAD_GATEWAY, "malformed gateway response"
                )
            message_content = message.get("content")
            if not isinstance(message_content, str):
                continue
            entry["message"] = {"content": message_content}
        if "content" in entry or "message" in entry:
            projected.append(entry)
    return projected


def _project_event(value: dict[str, object], bot_id: str) -> dict[str, object] | None:
    allowed_types: dict[str, type[object] | tuple[type[object], ...]] = {
        "agentId": str,
        "busy": bool,
        "content": str,
        "done": bool,
        "error": str,
        "id": str,
        "isBusy": bool,
        "kind": str,
        "state": str,
        "text": str,
        "timestampMs": (int, float),
    }

    def project_payload(payload: dict[str, object]) -> dict[str, object] | None:
        projected: dict[str, object] = {}
        for key, expected in allowed_types.items():
            if key not in payload:
                continue
            field = payload[key]
            if not isinstance(field, expected):
                return None
            if key == "timestampMs" and isinstance(field, bool):
                return None
            projected[key] = field
        return projected

    if value.get("agentId") == bot_id:
        return project_payload(value)
    payload = value.get("payload")
    if not _is_object_dict(payload) or payload.get("agentId") != bot_id:
        return None
    projected_payload = project_payload(payload)
    if projected_payload is None:
        return None
    result: dict[str, object] = {"payload": projected_payload}
    channel = value.get("channel")
    if isinstance(channel, str):
        result["channel"] = channel
    return result


def _sse(event: str, data: dict[str, object]) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def _next_event(feed: ShareEventFeed, timeout_s: float) -> dict[str, object] | None:
    try:
        return feed.next_event(timeout_s)
    except StopIteration:
        return None


def create_share_app(
    manager_factory: Callable[[], ShareManager],
    store: ShareAuthenticator,
    exec_factory: Callable[[ShareManager], ExecRunner] | None = None,
    *,
    event_heartbeat_s: float = 15.0,
) -> FastAPI:
    """Create the authenticated relay without exposing owner credentials."""
    app = FastAPI(title="Groken Share Relay", version="1.0.0")

    def authenticate_share(
        authorization: Annotated[str | None, Header()] = None,
    ) -> _ShareContext:
        if authorization is None or not authorization.startswith("Bearer "):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid share token")
        token = authorization.removeprefix("Bearer ")
        record = store.authenticate(token)
        if record is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid share token")
        return _ShareContext(record, manager_factory, token)

    def require_share(
        context: Annotated[_ShareContext, Depends(authenticate_share)],
    ) -> Iterator[_ShareContext]:
        try:
            yield context
        finally:
            context.close()

    async def managed_stream(
        context: _ShareContext, frames: AsyncIterator[str]
    ) -> AsyncIterator[str]:
        try:
            async for frame in frames:
                yield frame
        finally:
            context.close()

    def revalidate(context: _ShareContext) -> None:
        if store.authenticate(context.token) != context.record:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "share revoked")

    async def health() -> dict[str, bool]:
        return {"ok": True}

    async def bot(
        context: Annotated[_ShareContext, Depends(require_share)],
    ) -> dict[str, str]:
        return {"agent_id": context.record.bot_id, "name": context.record.bot_name}

    async def send(
        request: TextRequest, context: Annotated[_ShareContext, Depends(require_share)]
    ) -> object:
        value = await run_sync(
            context.manager.send_prompt, context.record.bot_id, request.text
        )
        revalidate(context)
        return _project_send(value)

    async def ask(
        request: AskRequest, context: Annotated[_ShareContext, Depends(require_share)]
    ) -> dict[str, str]:
        reply = await run_sync(
            partial(
                context.manager.ask,
                context.record.bot_id,
                request.text,
                request.timeout_s,
            )
        )
        revalidate(context)
        return {"reply": reply}

    async def ask_stream_frames(
        request: AskRequest, context: _ShareContext
    ) -> AsyncIterator[str]:
        sender, receiver = create_memory_object_stream[StreamItem](100)

        async def produce() -> None:
            def emit(chunk: str) -> None:
                run_from_thread(sender.send, _StreamChunk(chunk))

            async with sender:
                try:
                    reply = await run_sync(
                        partial(
                            context.manager.ask_stream,
                            context.record.bot_id,
                            request.text,
                            request.timeout_s,
                            request.idle_s,
                            emit,
                        ),
                        abandon_on_cancel=True,
                    )
                except (ConnectError, httpx.HTTPError):
                    await sender.send(_StreamError("upstream stream failed"))
                    return
                await sender.send(_StreamDone(reply))

        async with create_task_group() as tasks:
            tasks.start_soon(produce)
            async with receiver:
                while True:
                    try:
                        with fail_after(event_heartbeat_s):
                            item = await receiver.receive()
                    except TimeoutError:
                        try:
                            revalidate(context)
                        except HTTPException:
                            yield _sse("error", {"detail": "share revoked"})
                            tasks.cancel_scope.cancel()
                            break
                        yield ": heartbeat\n\n"
                        continue
                    except EndOfStream:
                        break
                    try:
                        revalidate(context)
                    except HTTPException:
                        yield _sse("error", {"detail": "share revoked"})
                        tasks.cancel_scope.cancel()
                        break
                    match item:
                        case _StreamChunk(text=text):
                            yield _sse("chunk", {"text": text})
                        case _StreamDone(reply=reply):
                            yield _sse("done", {"reply": reply})
                        case _StreamError(detail=detail):
                            yield _sse("error", {"detail": detail})

    async def ask_stream(
        request: AskRequest,
        context: Annotated[_ShareContext, Depends(authenticate_share)],
    ) -> StreamingResponse:
        return StreamingResponse(
            managed_stream(context, ask_stream_frames(request, context)),
            media_type="text/event-stream",
        )

    async def transcript(
        context: Annotated[_ShareContext, Depends(require_share)],
    ) -> object:
        value = await run_sync(context.manager.transcript_tail, context.record.bot_id)
        revalidate(context)
        return _project_transcript(value)

    async def execute(
        request: ExecRequest, context: Annotated[_ShareContext, Depends(require_share)]
    ) -> dict[str, str | int]:
        if exec_factory is None:
            async with ExecServiceClient(context.manager) as runner:
                result = await runner.execute(
                    request.command, request.cwd, request.timeout_ms
                )
        else:
            runner = exec_factory(context.manager)
            result = await runner.execute(
                request.command, request.cwd, request.timeout_ms
            )
        revalidate(context)
        return {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "exit_code": result.exit_code,
        }

    async def vnc(
        _request: VncRequest,
        context: Annotated[_ShareContext, Depends(require_share)],
    ) -> dict[str, str]:
        args: dict[str, object] = {"id": context.record.bot_id}
        box = await run_sync(
            partial(context.manager.command, "getForeverBoxStatus", args)
        )
        if not _is_object_dict(box):
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY, "malformed gateway response"
            )
        display = display_from_forever_box(box)
        if display is None:
            ensured = await run_sync(
                partial(context.manager.command, "ensureForeverBox", args)
            )
            if not _is_object_dict(ensured):
                raise HTTPException(
                    status.HTTP_502_BAD_GATEWAY, "malformed gateway response"
                )
            display = display_from_forever_box(ensured)
        if display is None:
            raise HTTPException(
                status.HTTP_409_CONFLICT, "shared Bot has no available display"
            )
        metadata = await run_sync(context.manager.ensure_sandbox_metadata)
        url = vnc_url(metadata, display=display, ttl_s=60)
        revalidate(context)
        return {"url": url}

    async def command(
        request: CommandRequest,
        context: Annotated[_ShareContext, Depends(require_share)],
    ) -> object:
        if request.method not in _ALLOWED_COMMANDS:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "command is unavailable")
        args = {**request.args, "id": context.record.bot_id}
        result = await run_sync(partial(context.manager.command, request.method, args))
        revalidate(context)
        if not isinstance(result, list) or not all(
            _is_object_dict(agent) for agent in result
        ):
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY, "malformed gateway response"
            )
        projected: list[dict[str, str]] = []
        for agent in result:
            agent_id, name = agent.get("id"), agent.get("name")
            if not isinstance(agent_id, str) or not isinstance(name, str):
                raise HTTPException(
                    status.HTTP_502_BAD_GATEWAY, "malformed gateway response"
                )
            if agent_id == context.record.bot_id:
                projected.append({"id": agent_id, "name": name})
        return projected

    async def event_frames(
        context: _ShareContext, channels: list[str] | None
    ) -> AsyncIterator[str]:
        selected = channels or []
        with context.manager.event_subscription(selected, None) as feed:
            while True:
                try:
                    envelope = await run_sync(_next_event, feed, event_heartbeat_s)
                    if envelope is None:
                        break
                except TimeoutError:
                    try:
                        revalidate(context)
                    except HTTPException:
                        break
                    yield ": heartbeat\n\n"
                    continue
                try:
                    revalidate(context)
                except HTTPException:
                    break
                event, data = envelope.get("event"), envelope.get("data")
                if (
                    not isinstance(event, str)
                    or not event
                    or "\n" in event
                    or "\r" in event
                    or not _is_object_dict(data)
                ):
                    continue
                safe_data = _project_event(data, context.record.bot_id)
                if safe_data is not None:
                    yield _sse(event, safe_data)

    async def events(
        context: Annotated[_ShareContext, Depends(authenticate_share)],
        channels: str | None = None,
    ) -> StreamingResponse:
        selected = (
            [channel.strip() for channel in channels.split(",") if channel.strip()]
            if channels is not None
            else None
        )
        return StreamingResponse(
            managed_stream(context, event_frames(context, selected)),
            media_type="text/event-stream",
        )

    app.add_api_route("/v1/health", health, methods=["GET"])
    app.add_api_route("/v1/bot", bot, methods=["GET"])
    app.add_api_route("/v1/send", send, methods=["POST"])
    app.add_api_route("/v1/ask", ask, methods=["POST"])
    app.add_api_route("/v1/ask/stream", ask_stream, methods=["POST"])
    app.add_api_route("/v1/transcript", transcript, methods=["GET"])
    app.add_api_route("/v1/exec", execute, methods=["POST"])
    app.add_api_route("/v1/vnc", vnc, methods=["POST"])
    app.add_api_route("/v1/command", command, methods=["POST"], response_model=None)
    app.add_api_route("/v1/events", events, methods=["GET"])
    return app
