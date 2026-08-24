import pytest

from groken.vnc import mint_jwt, vnc_url


def test_mint_jwt_hmac_vector():
    token = mint_jwt("secret", "tenant", "pod", now=1700000000)
    assert token == (
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
        "eyJhdWQiOiJ0ZW5hbnQiLCJleHAiOjE3MDAwMDA2MDAsIm5iZiI6MTY5OTk5OTk5MCwicG9kX2lkIjoicG9kIiwiY29udGFpbmVyX3BvcnQiOjYwODAsImlhdCI6MTcwMDAwMDAwMH0."
        "zPv3o6XfCeelwYZKvQy23VT1bqUjxtl3ylxp3agR8nc"
    )


def test_url_uses_metadata_host_and_does_not_require_token_output():
    url = vnc_url({"vncUrl": "https://tenant-pod-6080.us.cursorvm.com/vnc.html", "networkToken": "secret", "podId": "pod"}, 1700000000)
    assert url.startswith("https://tenant-pod-6080.us.cursorvm.com/vnc.html?port_token=")
    assert "secret" not in url


def test_missing_vnc_url_is_clear():
    with pytest.raises(ValueError, match="missing vncUrl"):
        vnc_url({"networkToken": "x", "podId": "p"})
