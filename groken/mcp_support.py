from __future__ import annotations

import functools
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated, Final, ParamSpec, TypeAlias, TypeVar

import httpx
from pydantic import BeforeValidator, WithJsonSchema

from .auth import TokenStateError
from .client import ConnectError
from .config import ConfigStateError
from .env_collectors import NativePlaneUnavailable
from .env_manifest import CaptureError
from .env_persistence import PersistenceError
from .env_restore_errors import JournalConflictError, JournalUnsafeError
from .env_restore_manifest import RestoreManifestError
from .env_restore_run import RestorePendingError
from .env_restore_validation import RestoreInputError
from .gateway import BotUpdateError
from .native_teams import NativeTeamError
from .routines import RoutineError
from .swarm_rooms import SwarmError
from .swarm_worker import WorkerProtocolError

P = ParamSpec("P")
R = TypeVar("R")
JsonValue: TypeAlias = (
    None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]
)
CONFIRMATION_REQUIRED: Final = (
    "Operation blocked: review the exact operation with the user, then retry with "
    "confirmed=true."
)
_LOCAL_TOOL_ERRORS: Final = (
    TokenStateError,
    ConfigStateError,
    RoutineError,
    PersistenceError,
    CaptureError,
    JournalUnsafeError,
    JournalConflictError,
    RestoreManifestError,
    RestorePendingError,
    RestoreInputError,
    NativePlaneUnavailable,
    SwarmError,
    WorkerProtocolError,
    BotUpdateError,
    NativeTeamError,
)


@dataclass(frozen=True, slots=True)
class ConfirmationError(TypeError):
    """A sanitized confirmation-boundary failure."""

    def __post_init__(self) -> None:
        TypeError.__init__(self, "confirmed must be a boolean")


def require_confirmation(value: JsonValue) -> bool:
    """Parse one exact JSON boolean without coercion or value-bearing errors."""
    if type(value) is not bool:
        raise ConfirmationError
    return value


Confirmation: TypeAlias = Annotated[
    bool,
    BeforeValidator(require_confirmation),
    WithJsonSchema({"type": "boolean"}),
]


def _safe_connect_error(tool_name: str, exc: ConnectError) -> str:
    category = {
        0: "network",
        401: "authentication",
        403: "authorization",
        404: "unavailable",
        408: "timeout",
        429: "rate-limit",
        500: "service",
        502: "service",
        503: "service",
        504: "service",
    }.get(exc.status, "gateway")
    return f"{tool_name} failed: gateway status {exc.status} ({category})."


def _safe_local_error(tool_name: str) -> str:
    return f"{tool_name} failed: local environment error."


def translate_tool_errors(fn: Callable[P, R]) -> Callable[P, R | str]:
    """Translate expected gateway and CLI failures into MCP-safe text results."""

    @functools.wraps(fn)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R | str:
        try:
            return fn(*args, **kwargs)
        except ConnectError as exc:
            return _safe_connect_error(fn.__name__, exc)
        except httpx.TimeoutException:
            return f"{fn.__name__} failed: network timeout."
        except httpx.HTTPError:
            return f"{fn.__name__} failed: network transport error."
        except _LOCAL_TOOL_ERRORS:
            return _safe_local_error(fn.__name__)
        except SystemExit:
            return f"{fn.__name__} failed: local configuration error."

    return wrapper


def translate_async_tool_errors(
    fn: Callable[P, Awaitable[R]],
) -> Callable[P, Awaitable[R | str]]:
    """Translate expected asynchronous gateway failures into MCP-safe text results."""

    @functools.wraps(fn)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R | str:
        try:
            return await fn(*args, **kwargs)
        except ConnectError as exc:
            return _safe_connect_error(fn.__name__, exc)
        except httpx.TimeoutException:
            return f"{fn.__name__} failed: network timeout."
        except httpx.HTTPError:
            return f"{fn.__name__} failed: network transport error."
        except _LOCAL_TOOL_ERRORS:
            return _safe_local_error(fn.__name__)
        except SystemExit:
            return f"{fn.__name__} failed: local configuration error."

    return wrapper
