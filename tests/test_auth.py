import base64
import hashlib
import json
import os
import time
from pathlib import Path
from types import TracebackType
from typing import Protocol, Self, final

import httpx
import pytest

from groken import auth


class _Response(Protocol):
    status_code: int

    def json(self) -> dict[str, object]: ...


@final
class _FakeClient:
    def __init__(self, response: _Response) -> None:
        self._response = response

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc_value: BaseException | None,
        _traceback: TracebackType | None,
    ) -> bool:
        return False

    def get(
        self,
        url: str,
        *,
        params: dict[str, str],
        headers: dict[str, str],
    ) -> _Response:
        assert url == f"{auth.API_BASE_URL}/auth/poll"
        assert params.keys() == {"uuid", "verifier"}
        assert headers == {"Content-Type": "application/json"}
        return self._response


@final
class _FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def _install_client(
    monkeypatch: pytest.MonkeyPatch, response: _Response
) -> None:
    def make_client(*, timeout: float) -> _FakeClient:
        assert timeout == 15.0
        return _FakeClient(response)

    monkeypatch.setattr(httpx, "Client", make_client)


def test_start_login_pkce_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(auth, "TOKEN_FILE", tmp_path / "tokens.json")
    params = auth.start_login()
    assert params["login_url"].startswith("https://cursor.com/loginDeepControl?")
    assert "redirectTarget=sand" in params["login_url"]
    expected_challenge = base64.urlsafe_b64encode(
        hashlib.sha256(params["verifier"].encode()).digest()
    ).decode().rstrip("=")
    assert f"challenge={expected_challenge}" in params["login_url"]
    assert f"uuid={params['uuid']}" in params["login_url"]


def test_token_roundtrip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auth, "TOKEN_FILE", tmp_path / "tokens.json")
    auth.save_tokens({"accessToken": "a", "refreshToken": "r"})
    assert auth.load_tokens() == {"accessToken": "a", "refreshToken": "r"}
    assert auth.get_access_token() == "a"
    mode = (tmp_path / "tokens.json").stat().st_mode & 0o777
    assert mode == 0o600


def test_save_tokens_repairs_existing_temporary_file_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token_file = tmp_path / "tokens.json"
    temporary = token_file.with_suffix(f"{token_file.suffix}.tmp")
    temporary.write_text("stale")
    temporary.chmod(0o644)
    monkeypatch.setattr(auth, "TOKEN_FILE", token_file)

    auth.save_tokens({"accessToken": "secret", "refreshToken": "refresh"})

    assert token_file.stat().st_mode & 0o777 == 0o600
    assert not temporary.exists()


@pytest.mark.parametrize(
    ("content", "reason"),
    [("{broken", "malformed JSON"), (json.dumps(["token"]), "expected a JSON object")],
)
def test_load_tokens_raises_typed_actionable_error_when_state_is_invalid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    content: str,
    reason: str,
) -> None:
    # Given
    token_file = tmp_path / "tokens.json"
    _ = token_file.write_text(content)
    monkeypatch.setattr(auth, "TOKEN_FILE", token_file)

    # When
    with pytest.raises(auth.TokenStateError) as raised:
        _ = auth.load_tokens()

    # Then
    assert raised.value.path == token_file
    assert raised.value.reason == reason
    assert "groken login" in str(raised.value)


def test_refresh_normalizes_snake_case(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(auth, "TOKEN_FILE", tmp_path / "tokens.json")
    auth.save_tokens({"accessToken": "old", "refreshToken": "rt"})

    @final
    class FakeResponse:
        status_code = 200

        def json(self) -> dict[str, object]:
            return {"access_token": "new", "refresh_token": "rt2"}

    def post(url: str, *, json: object, timeout: float) -> FakeResponse:
        assert url == f"{auth.API_BASE_URL}/oauth/token"
        assert isinstance(json, dict)
        assert timeout == 15.0
        return FakeResponse()

    monkeypatch.setattr(httpx, "post", post)
    merged = auth.refresh_tokens("rt")
    assert merged is not None
    assert merged["accessToken"] == "new"
    assert merged["refreshToken"] == "rt2"
    assert auth.get_access_token() == "new"


def test_poll_success_saves_and_returns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(auth, "TOKEN_FILE", tmp_path / "tokens.json")

    @final
    class FakeResponse:
        status_code = 200

        def json(self) -> dict[str, object]:
            return {"accessToken": "a", "refreshToken": "r"}

    _install_client(monkeypatch, FakeResponse())
    clock = _FakeClock()
    monkeypatch.setattr(time, "monotonic", clock.monotonic)
    monkeypatch.setattr(time, "sleep", clock.sleep)
    result = auth.poll_for_tokens("uid", "verifier")
    assert result == {"accessToken": "a", "refreshToken": "r"}
    assert auth.load_tokens() == {"accessToken": "a", "refreshToken": "r"}


def test_poll_timeout_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(auth, "TOKEN_FILE", tmp_path / "tokens.json")

    @final
    class FakeResponse:
        status_code = 500

        def json(self) -> dict[str, object]:
            return {}

    _install_client(monkeypatch, FakeResponse())
    clock = _FakeClock()
    monkeypatch.setattr(time, "monotonic", clock.monotonic)
    monkeypatch.setattr(time, "sleep", clock.sleep)
    assert auth.poll_for_tokens("uid", "verifier", timeout_s=0.01) is None
    assert auth.load_tokens() is None


def test_refresh_ignores_falsy_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(auth, "TOKEN_FILE", tmp_path / "tokens.json")
    auth.save_tokens({"accessToken": "old", "refreshToken": "rt"})

    @final
    class FakeResponse:
        status_code = 200

        def json(self) -> dict[str, object]:
            return {"access_token": "", "refresh_token": None}

    def post(url: str, *, json: object, timeout: float) -> FakeResponse:
        assert url == f"{auth.API_BASE_URL}/oauth/token"
        assert isinstance(json, dict)
        assert timeout == 15.0
        return FakeResponse()

    monkeypatch.setattr(httpx, "post", post)
    merged = auth.refresh_tokens("rt")
    assert merged is not None
    assert merged["accessToken"] == "old"
    assert merged["refreshToken"] == "rt"


def test_refresh_failure_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(auth, "TOKEN_FILE", tmp_path / "tokens.json")

    @final
    class FakeResponse:
        status_code = 400

        def json(self) -> dict[str, object]:
            return {}

    def post(url: str, *, json: object, timeout: float) -> FakeResponse:
        assert url == f"{auth.API_BASE_URL}/oauth/token"
        assert isinstance(json, dict)
        assert timeout == 15.0
        return FakeResponse()

    monkeypatch.setattr(httpx, "post", post)
    assert auth.refresh_tokens("rt") is None


def test_save_tokens_is_atomic_and_temp_file_is_private(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token_file = tmp_path / "tokens.json"
    monkeypatch.setattr(auth, "TOKEN_FILE", token_file)
    auth.save_tokens({"accessToken": "old", "refreshToken": "r"})

    def fail_replace(
        _source: str | os.PathLike[str], _destination: str | os.PathLike[str]
    ) -> None:
        raise OSError("simulated crash before replace")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated crash"):
        auth.save_tokens({"accessToken": "new", "refreshToken": "r2"})

    assert auth.load_tokens() == {"accessToken": "old", "refreshToken": "r"}
    temporary = token_file.with_suffix(f"{token_file.suffix}.tmp")
    assert temporary.is_file()
    assert temporary.stat().st_mode & 0o777 == 0o600
