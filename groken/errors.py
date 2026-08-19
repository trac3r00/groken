from .client import ConnectError


def explain_error(exc: Exception) -> str:
    if isinstance(exc, ConnectError):
        body = exc.body.lower()
        if exc.status == 401:
            return "Authentication expired. Run: groken login"
        if "could not be routed" in body:
            return "The cloud sandbox is unreachable or recovering. Retry in a minute; if it persists, run: groken doctor"
        if "unknown gateway method" in body:
            return "The Grok Bot app is newer than this bridge understands. Update the app, then re-check with: groken doctor"
        return f"Gateway error {exc.status}: {exc.body[:200]}"
    return str(exc)
