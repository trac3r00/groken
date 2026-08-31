from __future__ import annotations

import json

from .bot_update import GatewayUpdateBackend, UpdateOptions
from .env_manifest import capture_for_gateway
from .env_restore_gateway import RestoreCommandOptions
from .gateway import GatewayManager
from .gateway_operations import run_gateway_restore, run_gateway_update
from .mcp_support import (
    CONFIRMATION_REQUIRED,
    Confirmation,
    require_confirmation,
    translate_tool_errors,
)
from .routines import BUILTIN_TEMPLATES, RoutineEvent, list_routines, run_routine


class _BufferedConsole:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def write(self, line: str) -> None:
        self.lines.append(line)

    def prompt(self, message: str) -> str | None:
        del message
        return None

    @property
    def output(self) -> str:
        return "\n".join(self.lines)


@translate_tool_errors
def grok_bot_update_status(bot: str | None = None) -> str:
    """Read the selected Bot's host and image update availability without triggering an update. Omit bot to inspect the configured groken Bot."""
    backend = GatewayUpdateBackend(GatewayManager())
    bot_id = backend.resolve(bot)
    availability = backend.availability(bot_id)
    return json.dumps(
        {
            "bot": bot_id,
            "hostUpdateAvailable": availability.host,
            "imageUpdateAvailable": availability.image,
        },
        ensure_ascii=False,
        indent=2,
    )


@translate_tool_errors
def grok_bot_update_trigger(
    bot: str | None = None,
    skip_capture: bool = False,
    confirmed: Confirmation = False,
) -> str:
    """Capture the selected Bot's environment, trigger its manual computer update once, wait for readiness, and restore the environment. Set confirmed=true only after the user reviews the exact bot and skip_capture option."""
    if not require_confirmation(confirmed):
        return CONFIRMATION_REQUIRED
    console = _BufferedConsole()
    run_gateway_update(
        GatewayManager(),
        UpdateOptions(bot, yes=True, skip_capture=skip_capture),
        console,
    )
    return console.output


@translate_tool_errors
def grok_env_capture(bot: str | None = None) -> str:
    """Read the selected Bot's installed environment and save a content-addressed local manifest without installing, deleting, or changing software on the Bot."""
    outcome = capture_for_gateway(GatewayManager(), bot)
    return (
        f"source={outcome.source.value} manifest_id={outcome.manifest_id} "
        f"path={outcome.local_path}"
    )


@translate_tool_errors
def grok_env_restore(
    bot: str | None = None,
    retry_manual: bool = False,
    confirmed: Confirmation = False,
) -> str:
    """Plan and run the selected Bot's diff-based environment restore once without prompting. Set confirmed=true only after the user reviews the exact bot and retry_manual option."""
    if not require_confirmation(confirmed):
        return CONFIRMATION_REQUIRED
    console = _BufferedConsole()
    run_gateway_restore(
        GatewayManager(),
        RestoreCommandOptions(bot, yes=True, retry_manual=retry_manual),
        console,
    )
    return console.output


@translate_tool_errors
def grok_routine_list() -> str:
    """List built-in and stored routine names without creating, editing, or executing a routine."""
    names = {routine.name for routine in list_routines()}
    names.update(template.name for template in BUILTIN_TEMPLATES)
    return json.dumps(sorted(names), ensure_ascii=False, indent=2)


@translate_tool_errors
def grok_routine_run(
    name: str,
    event: RoutineEvent = RoutineEvent.MANUAL,
    confirmed: Confirmation = False,
) -> str:
    """Run one declared routine event exactly once. Set confirmed=true only after the user reviews the exact routine name and event."""
    if not require_confirmation(confirmed):
        return CONFIRMATION_REQUIRED
    exit_code = run_routine(name, event)
    return json.dumps(
        {"routine": name, "event": event.value, "exit_code": exit_code},
        ensure_ascii=False,
        sort_keys=True,
    )


OPERATION_TOOLS = (
    grok_bot_update_status,
    grok_bot_update_trigger,
    grok_env_capture,
    grok_env_restore,
    grok_routine_list,
    grok_routine_run,
)
