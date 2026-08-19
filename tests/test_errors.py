from groken.client import ConnectError
from groken.errors import explain_error


def test_auth_error_suggests_login():
    assert "groken login" in explain_error(ConnectError(401, "unauthenticated"))


def test_unroutable_suggests_retry():
    msg = explain_error(ConnectError(404, "The request could not be routed"))
    assert "sandbox" in msg and ("retry" in msg.lower() or "recover" in msg.lower())


def test_unknown_command_suggests_app_update():
    msg = explain_error(ConnectError(404, "unknown gateway method: foo"))
    assert "Grok Bot" in msg and "updat" in msg.lower()


def test_plain_error_passthrough():
    msg = explain_error(ValueError("unknown bot: nobody"))
    assert "unknown bot" in msg
