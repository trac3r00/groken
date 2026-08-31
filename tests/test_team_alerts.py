from pathlib import Path
from typing import Protocol

from fastapi import FastAPI
from fastapi.testclient import TestClient

from groken.controller_app import ControllerSettings, create_controller_app
from groken.team_alerts import (
    TeamAlertEmitter,
    TeamAlertSettings,
    create_team_alert_router,
)


class Emitter(Protocol):
    async def emit(
        self,
        team: str,
        message: str,
        client_nonce: str,
    ) -> None: ...


class FakeEmitter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    async def emit(
        self,
        team: str,
        message: str,
        client_nonce: str,
    ) -> None:
        self.calls.append((team, message, client_nonce))


def client_for(emitter: TeamAlertEmitter) -> TestClient:
    app = FastAPI()
    app.include_router(
        create_team_alert_router(
            TeamAlertSettings(
                bearer_token="alert-token-with-at-least-32-characters",
                team="github-monitor",
            ),
            emitter=emitter,
        )
    )
    return TestClient(app)


def test_team_alert_requires_scoped_bearer_and_idempotency_key() -> None:
    emitter = FakeEmitter()
    client = client_for(emitter)
    payload = {"source": "github-monitor-minpeter", "message": "new star"}

    assert client.post("/v1/team-alerts", json=payload).status_code == 401
    assert (
        client.post(
            "/v1/team-alerts",
            headers={"authorization": "Bearer alert-token-with-at-least-32-characters"},
            json=payload,
        ).status_code
        == 422
    )
    assert emitter.calls == []


def test_team_alert_posts_to_fixed_team_with_gateway_nonce() -> None:
    emitter = FakeEmitter()
    client = client_for(emitter)

    response = client.post(
        "/v1/team-alerts",
        headers={
            "authorization": "Bearer alert-token-with-at-least-32-characters",
            "idempotency-key": "github-event-123",
        },
        json={
            "source": "github-monitor-code-yeongyu",
            "message": "starred https://github.com/luxus/pi-hindsight",
        },
    )

    assert response.status_code == 202
    assert response.json() == {"accepted": True, "team": "github-monitor"}
    assert emitter.calls == [
        (
            "github-monitor",
            "[github-monitor-code-yeongyu] starred https://github.com/luxus/pi-hindsight",
            "github-event-123",
        )
    ]


def test_controller_exposes_configured_team_alert_route(tmp_path: Path) -> None:
    emitter = FakeEmitter()
    app = create_controller_app(
        ControllerSettings(
            state_dir=tmp_path,
            controller_token="controller-token",
            enrollment_token="enrollment-token",
            worker_token="alert-token-with-at-least-32-characters",
            model_base_url="https://model.example/v1",
            model_api_key="model-key",
            model="model-name",
            team_alert_team="github-monitor",
        ),
        team_alert_emitter=emitter,
    )

    response = TestClient(app).post(
        "/v1/team-alerts",
        headers={
            "authorization": "Bearer alert-token-with-at-least-32-characters",
            "idempotency-key": "controller-route-123",
        },
        json={"source": "github-monitor-minpeter", "message": "new repository"},
    )

    assert response.status_code == 202
    assert emitter.calls[0][0] == "github-monitor"
