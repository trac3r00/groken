from __future__ import annotations

import hmac
from dataclasses import dataclass
from typing import Annotated, Protocol, final

import httpx
from anyio.to_thread import run_sync
from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, ConfigDict, StringConstraints

from .client import ConnectError
from .gateway import GatewayManager

Source = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9._-]+$",
    ),
]
Message = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=4000),
]
IdempotencyKey = Annotated[
    str,
    Header(alias="Idempotency-Key", min_length=1, max_length=128),
]


class TeamAlertRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Source
    message: Message


@dataclass(frozen=True)
class TeamAlertSettings:
    bearer_token: str
    team: str


class TeamAlertEmitter(Protocol):
    async def emit(
        self,
        team: str,
        message: str,
        client_nonce: str,
    ) -> None: ...


@final
class GatewayTeamAlertEmitter:
    async def emit(
        self,
        team: str,
        message: str,
        client_nonce: str,
    ) -> None:
        def send() -> None:
            manager = GatewayManager()
            try:
                agent_id = manager.resolve_agent(team)
                _ = manager.send_prompt(agent_id, message, client_nonce)
            finally:
                manager.close()

        await run_sync(send)


def create_team_alert_router(
    settings: TeamAlertSettings,
    *,
    emitter: TeamAlertEmitter | None = None,
) -> APIRouter:
    active_emitter = emitter or GatewayTeamAlertEmitter()
    router = APIRouter()

    def require_alert(authorization: Annotated[str | None, Header()] = None) -> None:
        expected = f"Bearer {settings.bearer_token}"
        if not settings.bearer_token or authorization is None or not hmac.compare_digest(
            authorization,
            expected,
        ):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid team alert token")

    async def post_alert(
        request: TeamAlertRequest,
        idempotency_key: IdempotencyKey,
    ) -> dict[str, object]:
        message = f"[{request.source}] {request.message}"
        try:
            await active_emitter.emit(settings.team, message, idempotency_key)
        except (ConnectError, httpx.HTTPError, OSError, ValueError) as error:
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY,
                "team alert delivery failed",
            ) from error
        return {"accepted": True, "team": settings.team}

    router.add_api_route(
        "/v1/team-alerts",
        post_alert,
        methods=["POST"],
        status_code=status.HTTP_202_ACCEPTED,
        dependencies=[Depends(require_alert)],
    )
    return router
