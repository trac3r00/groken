import base64
import hashlib

from groken import auth


def test_start_login_pkce_shape(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "TOKEN_FILE", tmp_path / "tokens.json")
    params = auth.start_login()
    assert params["login_url"].startswith("https://cursor.com/loginDeepControl?")
    assert "redirectTarget=sand" in params["login_url"]
    expected_challenge = base64.urlsafe_b64encode(
        hashlib.sha256(params["verifier"].encode()).digest()
    ).decode().rstrip("=")
    assert f"challenge={expected_challenge}" in params["login_url"]
    assert f"uuid={params['uuid']}" in params["login_url"]


def test_token_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "TOKEN_FILE", tmp_path / "tokens.json")
    auth.save_tokens({"accessToken": "a", "refreshToken": "r"})
    assert auth.load_tokens() == {"accessToken": "a", "refreshToken": "r"}
    assert auth.get_access_token() == "a"
    mode = (tmp_path / "tokens.json").stat().st_mode & 0o777
    assert mode == 0o600


def test_refresh_normalizes_snake_case(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "TOKEN_FILE", tmp_path / "tokens.json")
    auth.save_tokens({"accessToken": "old", "refreshToken": "rt"})

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"access_token": "new", "refresh_token": "rt2"}

    monkeypatch.setattr(auth.httpx, "post", lambda *a, **k: FakeResponse())
    merged = auth.refresh_tokens("rt")
    assert merged is not None
    assert merged["accessToken"] == "new"
    assert merged["refreshToken"] == "rt2"
    assert auth.get_access_token() == "new"


class _FakeClient:
    def __init__(self, response):
        self._response = response

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def get(self, *args, **kwargs):
        return self._response


def test_poll_success_saves_and_returns(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "TOKEN_FILE", tmp_path / "tokens.json")

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"accessToken": "a", "refreshToken": "r"}

    monkeypatch.setattr(auth.httpx, "Client", lambda *a, **k: _FakeClient(FakeResponse()))
    monkeypatch.setattr(auth.time, "sleep", lambda *_: None)
    result = auth.poll_for_tokens("uid", "verifier")
    assert result == {"accessToken": "a", "refreshToken": "r"}
    assert auth.load_tokens() == {"accessToken": "a", "refreshToken": "r"}


def test_poll_timeout_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "TOKEN_FILE", tmp_path / "tokens.json")

    class FakeResponse:
        status_code = 500

        def json(self):
            return {}

    monkeypatch.setattr(auth.httpx, "Client", lambda *a, **k: _FakeClient(FakeResponse()))
    monkeypatch.setattr(auth.time, "sleep", lambda *_: None)
    assert auth.poll_for_tokens("uid", "verifier", timeout_s=0.01) is None
    assert auth.load_tokens() is None


def test_refresh_ignores_falsy_fields(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "TOKEN_FILE", tmp_path / "tokens.json")
    auth.save_tokens({"accessToken": "old", "refreshToken": "rt"})

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"access_token": "", "refresh_token": None}

    monkeypatch.setattr(auth.httpx, "post", lambda *a, **k: FakeResponse())
    merged = auth.refresh_tokens("rt")
    assert merged is not None
    assert merged["accessToken"] == "old"
    assert merged["refreshToken"] == "rt"


def test_refresh_failure_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "TOKEN_FILE", tmp_path / "tokens.json")

    class FakeResponse:
        status_code = 400

        def json(self):
            return {}

    monkeypatch.setattr(auth.httpx, "post", lambda *a, **k: FakeResponse())
    assert auth.refresh_tokens("rt") is None
