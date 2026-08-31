"""Versioned Grok Bot update command adapter and readiness handling."""

from __future__ import annotations

import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from enum import StrEnum
from typing import assert_never, final

import httpx

from .client import ConnectError
from .env_persistence import is_record
from .gateway import (
    UpdateAvailability,
    UpdateBoxState,
    UpdateEventFeed,
    UpdateGateway,
    UpdateIndeterminateError,
    UpdateReadinessError,
    update_box_state,
)


class UpdateKind(StrEnum):
    IMAGE = "image"
    HOST = "host"


def select_update_kind(availability: UpdateAvailability) -> UpdateKind | None:
    """Match the official UI: image updates take precedence over host updates."""
    if availability.image is True:
        return UpdateKind.IMAGE
    if availability.host is True:
        return UpdateKind.HOST
    return None


@final
class GatewayUpdateBackend:
    def __init__(
        self, gateway: UpdateGateway, monotonic: Callable[[], float] = time.monotonic
    ) -> None:
        self._gateway = gateway
        self._monotonic = monotonic
        self._events: UpdateEventFeed | None = None
        self._baseline: UpdateBoxState | None = None
        self._timeout_s = 0.0
        self._triggered = False

    def resolve(self, bot: str | None) -> str:
        return self._gateway.resolve_agent(bot)

    def availability(self, bot_id: str) -> UpdateAvailability:
        value = self._gateway.command("getForeverBoxStatus", {"id": bot_id})
        record = value if is_record(value) else {}
        host, image = (
            record.get("hostUpdateAvailable"),
            record.get("imageUpdateAvailable"),
        )
        return UpdateAvailability(
            host=host if isinstance(host, bool) else None,
            image=image if isinstance(image, bool) else None,
        )

    def _next_state(
        self, bot_id: str, deadline: float, *, hold: bool = False
    ) -> UpdateBoxState:
        if self._events is None:
            raise UpdateReadinessError("readiness event feed is not active")
        while True:
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                raise UpdateReadinessError("update readiness timed out")
            try:
                frame = self._events.next_event(remaining, hold=hold)
            except (TimeoutError, httpx.TimeoutException) as exc:
                raise UpdateReadinessError("update readiness timed out") from exc
            except (StopIteration, ConnectError, httpx.TransportError) as exc:
                raise UpdateReadinessError(
                    "update readiness disconnected before the Bot was ready"
                ) from exc
            state = update_box_state(frame, bot_id)
            if state is not None:
                return state
            if hold:
                self._events.resume()

    @contextmanager
    def subscribe(self, bot_id: str, timeout_s: float) -> Generator[None, None, None]:
        if self._events is not None:
            raise UpdateReadinessError(
                f"readiness subscription already active for {bot_id}"
            )
        with self._gateway.event_subscription(["forever-box"], timeout_s) as events:
            self._events, self._timeout_s, self._triggered = events, timeout_s, False
            try:
                self._baseline = self._next_state(
                    bot_id, self._monotonic() + timeout_s, hold=True
                )
                yield
            finally:
                self._events, self._baseline, self._triggered = None, None, False

    def trigger(self, bot_id: str, kind: UpdateKind) -> None:
        if self._triggered:
            raise UpdateIndeterminateError(bot_id)
        self._triggered = True
        try:
            match kind:
                case UpdateKind.IMAGE:
                    if self._events is None:
                        raise UpdateReadinessError(
                            "image update requires an active readiness subscription"
                        )
                    result = self._gateway.command_once(
                        "updateForeverBox", {"id": bot_id}
                    )
                case UpdateKind.HOST:
                    result = self._gateway.command_once(
                        "updateHostNow", {"force": True}
                    )
                case _ as unreachable:
                    assert_never(unreachable)
        except httpx.TransportError as exc:
            raise UpdateIndeterminateError(bot_id) from exc
        match kind:
            case UpdateKind.IMAGE:
                if not is_record(result):
                    raise UpdateReadinessError(
                        "image update returned an invalid box status"
                    )
                if self._events is not None:
                    self._events.resume()
            case UpdateKind.HOST:
                if not is_record(result) or result.get("started") is not True:
                    raise UpdateReadinessError("host update did not start")
            case _ as unreachable:
                assert_never(unreachable)

    def wait_ready(self, bot_id: str) -> None:
        if self._events is None or self._baseline is None or not self._triggered:
            raise UpdateReadinessError(
                "readiness wait started without a triggered subscription"
            )
        deadline, transitioned = self._monotonic() + self._timeout_s, False
        while True:
            state = self._next_state(bot_id, deadline)
            transitioned = transitioned or not state.ready or state != self._baseline
            if transitioned and state.ready:
                return
