import argparse
import asyncio
import getpass
import json
import os
import shlex
import subprocess
import sys
import threading
import uuid
import webbrowser
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Literal, Protocol, cast, overload

from .auth import load_tokens, poll_for_tokens, refresh_tokens, start_login
from .client import SandClient
from .exec_service import ExecServiceClient, ExecServiceError
from .routines import (
    BUILTIN_TEMPLATES,
    RoutineError,
    RoutineEvent,
    edit_path,
    list_routines,
    new_routine,
    run_routine,
)


class _GatewayEventFeed(Protocol):
    def next_event(
        self, timeout_s: float | None, *, hold: bool = False
    ) -> dict[str, object]: ...
    def resume(self) -> None: ...


class _GatewayManager(Protocol):
    @overload
    def command(
        self, method: Literal["listAgents"], args: None = None
    ) -> list[dict[str, object]]: ...

    @overload
    def command(
        self,
        method: Literal["getForeverBoxStatus", "ensureForeverBox"],
        args: dict[str, object],
    ) -> dict[str, object]: ...

    @overload
    def command(self, method: str, args: dict[str, object] | None = None) -> object: ...

    def command_once(
        self, method: str, args: dict[str, object] | None = None
    ) -> object: ...
    def event_subscription(
        self, channels: list[str], timeout_s: float | None
    ) -> AbstractContextManager[_GatewayEventFeed]: ...
    def create_bot(self, name: str) -> dict[str, object]: ...
    def duplicate_bot(self, source_name: str, name: str) -> dict[str, object]: ...
    def ensure_sandbox_metadata(self) -> dict[str, object]: ...
    def events(self) -> Iterator[dict[str, object]]: ...
    def own_agent_id(self) -> str: ...
    def resolve_agent(self, bot: str | None = None) -> str: ...
    def send_prompt(self, agent_id: str, text: str) -> dict[str, object]: ...
    def transcript_tail(self, agent_id: str) -> list[dict[str, object]]: ...
    def ask(self, agent_id: str, text: str, timeout_s: float = 600) -> str: ...
    def ask_stream(
        self,
        agent_id: str,
        text: str,
        timeout_s: float = 600,
        idle_s: float = 45,
        on_chunk: Callable[[str], None] | None = None,
    ) -> str: ...


class _BotRosterManager(Protocol):
    def command(self, method: str, args: dict[str, object] | None = None) -> object: ...


class _CliArgs(argparse.Namespace):
    def __init__(self) -> None:
        super().__init__()
        self.cmd: str | None = None
        self.agents: list[str] = []
        self.use_all: bool = False
        self.dry_run: bool = False
        self.bot: str | None = None
        self.display: int | None = None
        self.tools_cmd: str = ""
        self.servers: list[str] = []
        self.as_json: bool = False
        self.server: str = ""
        self.tool: str = ""
        self.args_json: str = "{}"
        self.yes: bool = False
        self.open_browser: bool = False
        self.agent: str | None = None
        self.text: str = ""
        self.limit: int = 15
        self.since: str | None = None
        self.full: bool = False
        self.timeout: float = 600.0
        self.stream: bool = False
        self.command: str = ""
        self.cwd: str = "/workspace"
        self.timeout_ms: int = 15000
        self.service_cmd: str = ""
        self.bot_cmd: Literal["add", "duplicate", "env", "update"] | None = None
        self.bot_env_cmd: Literal["capture", "restore"] | None = None
        self.bot_name: str = ""
        self.source_bot: str = ""
        self.team_cmd: Literal["create", "members", "ask"] | None = None
        self.team_name: str = ""
        self.team_bots: str = ""
        self.team_description: str = ""
        self.env_bot: str | None = None
        self.update_bot: str | None = None
        self.skip_capture: bool = False
        self.retry_manual: bool = False
        self.routine_cmd: Literal["list", "new", "edit", "run"] | None = None
        self.routine_name: str = ""
        self.routine_event: RoutineEvent = RoutineEvent.MANUAL
        self.app_path: str | None = None
        self.fail_on_drift: bool = False
        self.share_cmd: (
            Literal[
                "create", "list", "revoke", "serve", "connect", "disconnect", "status"
            ]
            | None
        ) = None
        self.share_name: str = ""
        self.share_bot: str = ""
        self.share_url: str = ""
        self.share_token_file: str | None = None
        self.share_host: str = "127.0.0.1"
        self.share_port: int = 8787
        self.swarm_cmd: Literal["send", "rooms"] | None = None
        self.swarm_bots: str | None = None
        self.swarm_exclude: str | None = None
        self.swarm_timeout_s: float = 600.0
        self.swarm_rounds: int = 1


def _manager() -> _GatewayManager:
    from .share_client import RelayManager, load_share_link

    link = load_share_link()
    if link is not None:
        return RelayManager(link)
    from .gateway import GatewayManager

    return cast("_GatewayManager", cast(object, GatewayManager()))


_SHARED_COMMANDS = frozenset(
    {"ask", "send", "tail", "events", "exec", "vnc", "connect"}
)
_SHARED_ADMIN_COMMANDS = frozenset({"connect", "disconnect", "status"})


def _enforce_share_mode(args: _CliArgs) -> None:
    from .share_client import load_share_link

    if load_share_link() is None:
        return
    if args.cmd in _SHARED_COMMANDS:
        return
    if args.cmd == "share" and args.share_cmd in _SHARED_ADMIN_COMMANDS:
        return
    sys.exit(
        f"{args.cmd}: unavailable while connected to a share; "
        "run 'groken share disconnect' first"
    )


def cmd_login() -> None:
    params = start_login()
    print("Open this URL and sign in with the account that owns Grok Bot:\n")
    print(params["login_url"], "\n")
    try:
        _ = webbrowser.open(params["login_url"])
    except (webbrowser.Error, OSError):
        # Browser launch is best-effort; the URL printed above is the fallback.
        pass
    print("Waiting for sign-in to complete (up to 5 min)...")
    tokens = poll_for_tokens(params["uuid"], params["verifier"])
    if not tokens:
        sys.exit("Login timed out or failed.")
    print("Signed in. Tokens saved to ~/.config/groken/tokens.json")


GUIDE = """groken — talk to Grok Bot cloud agents from your terminal or any AI agent.

First run:
  groken login              sign in (opens cursor.com in your browser)
  groken install            optionally connect detected AI-agent harnesses
  groken doctor             verify setup and auto-create the default groken Bot
  groken ask "task"         send a task and wait for the verified reply

Use an existing Bot instead:
  groken list               list Bots (* marks this machine's default)
  groken configure NAME     choose an existing Bot by name or id

Everyday use:
  groken connect [bot]      open the configured or named Bot's computer
  groken send "task"        fire-and-forget
  groken tail               recent conversation
  groken status             Bot, host, secrets, MCP, and local health
  groken capabilities       official 0.30 gateway inventory + safe live status
  groken inspect-app        diff the installed app's command table vs groken

Run `groken guide` to show this again, or any command with --help for options."""


def _select_agents(candidates: list[str], action: str) -> list[str]:
    print(f"Detected agents available to {action}:\n")
    for i, name in enumerate(candidates, 1):
        print(f"  {i:>2}. {name}")
    print("\nEnter numbers (e.g. 1 3 5), 'a' for all, or press Enter to cancel.")
    raw = input("> ").strip()
    if not raw:
        return []
    if raw.lower() in {"a", "all"}:
        return candidates
    chosen: list[str] = []
    for token in raw.replace(",", " ").split():
        if token.isdigit() and 1 <= int(token) <= len(candidates):
            chosen.append(candidates[int(token) - 1])
        elif token in candidates:
            chosen.append(token)
        else:
            sys.exit(f"unrecognized selection: {token}")
    return chosen


def _resolve_selection(
    agents: list[str], use_all: bool, candidates: list[str], action: str
) -> list[str]:
    if agents:
        return agents
    if use_all:
        return candidates
    if not sys.stdin.isatty():
        sys.exit(
            "no agents selected. Run `groken install --all` or pass target names "
            f"from `groken install --help`. Detected: {', '.join(candidates) or 'none'}"
        )
    if not candidates:
        sys.exit("no supported AI agents detected on this machine.")
    return _select_agents(candidates, action)


def _find_bot(
    agents: list[dict[str, object]], selector: str
) -> dict[str, object] | None:
    return next(
        (
            agent
            for agent in agents
            if agent.get("id") == selector or agent.get("name") == selector
        ),
        None,
    )


def cmd_configure(bot: str | None) -> None:
    from .config import remember_bot
    from .installers import install_cli_command

    agents = list(_manager().command("listAgents"))
    if not agents:
        sys.exit("configure: no Bots found")
    selected: dict[str, object] | None = None
    attempted = bot
    if bot is not None:
        selected = _find_bot(agents, bot)
    elif not sys.stdin.isatty():
        sys.exit(
            "configure: pass a Bot name or id when not interactive; run "
            "`groken list`, then `groken configure NAME`"
        )
    else:
        print("Choose the default Bot for this machine:\n")
        for index, agent in enumerate(agents, 1):
            print(f"  {index}. {agent.get('name', '?')}  ({agent.get('id', '?')})")
        raw = input("Select Bot: ").strip()
        attempted = raw
        if not raw:
            sys.exit("configure: cancelled")
        if raw.isdigit() and 1 <= int(raw) <= len(agents):
            selected = agents[int(raw) - 1]
        else:
            selected = _find_bot(agents, raw)
    if selected is None:
        sys.exit(f"configure: unknown Bot: {attempted or ''}")
    bot_id = str(selected["id"])
    name = str(selected.get("name") or bot_id)
    remember_bot(bot_id, name)
    command_outcome = install_cli_command(dry_run=False)
    if command_outcome.startswith("failed"):
        sys.exit(f"configure: {command_outcome}")
    print(f"Configured Bot: {name} ({bot_id})")
    print(f"CLI command: {command_outcome}")


def cmd_vnc(
    _open_browser: bool,
    display: int | None = None,
    bot: str | None = None,
) -> None:
    from .share_client import RelayManager, ShareRemoteError
    from .vnc import display_from_forever_box, vnc_url
    from .vnc_proxy import serve_vnc_proxy
    from .vnc_ready import VncNotReadyError, wait_until_vnc_ready

    manager = _manager()
    bot_id = manager.own_agent_id() if bot is None else manager.resolve_agent(bot)
    selected_display: int | None = None
    try:
        if isinstance(manager, RelayManager):
            if display is not None:
                sys.exit("vnc: explicit displays are unavailable through a share")
            url = manager.vnc_url()
        else:
            selected_display = display
            if selected_display is None:
                status = manager.command("getForeverBoxStatus", {"id": bot_id})
                selected_display = display_from_forever_box(status)
                if selected_display is None:
                    status = manager.command("ensureForeverBox", {"id": bot_id})
                    selected_display = display_from_forever_box(status)
                if selected_display is None:
                    sys.exit("vnc: configured bot has no available computer display")
            url = vnc_url(manager.ensure_sandbox_metadata(), display=selected_display)
    except (ValueError, ShareRemoteError) as exc:
        sys.exit(f"vnc: {exc}")
    try:
        session = serve_vnc_proxy(url)
    except ValueError as exc:
        sys.exit(f"vnc: {exc}")
    label = "configured bot" if bot is None else "bot"
    display_label = "" if selected_display is None else f" display :{selected_display}"
    print(f"Using {label} {bot_id}{display_label}.", file=sys.stderr)
    print("Waiting for VNC desktop...", file=sys.stderr)
    try:
        wait_until_vnc_ready(session)
    except KeyboardInterrupt:
        session.close()
        sys.exit(130)
    except VncNotReadyError as exc:
        session.close()
        sys.exit(f"vnc: {exc}")
    print(session.local_url)
    try:
        _ = webbrowser.open(session.local_url)
    except (webbrowser.Error, OSError):
        pass
    try:
        _ = threading.Event().wait()
    except KeyboardInterrupt:
        session.close()


def cmd_install(agents: list[str], dry_run: bool, use_all: bool) -> None:
    from .config import load_config, set_vnc_enabled
    from .installers import detected_agents, install_all, install_cli_command

    command_outcome = install_cli_command(dry_run=dry_run)
    if command_outcome.startswith("failed"):
        sys.exit(f"install: {command_outcome}")
    print(f"+ command  {command_outcome}")
    if not dry_run and "vnc" not in load_config() and sys.stdin.isatty():
        answer = input("Enable groken vnc? [y/N] ").strip().lower()
        set_vnc_enabled(answer in {"y", "yes"})
    candidates = [] if agents else detected_agents()
    selection = _resolve_selection(agents, use_all, candidates, "install into")
    if not selection:
        print("nothing selected.")
        return
    try:
        results = install_all(dry_run=dry_run, only=selection)
    except ValueError as e:
        sys.exit(str(e))
    width = max(len(n) for n in results)
    for name, outcome in results.items():
        mark = "-" if outcome.startswith("skipped") else "+"
        print(f"{mark} {name:<{width}}  {outcome}")
    failures = [
        f"{name}: {outcome}"
        for name, outcome in results.items()
        if outcome.startswith("failed")
    ]
    if failures:
        sys.exit(f"install failed: {'; '.join(failures)}")
    if not dry_run:
        print("\nRestart the agent app/CLI to pick up the new MCP server.")


def cmd_uninstall(agents: list[str], dry_run: bool, use_all: bool) -> None:
    from .installers import UNINSTALLERS, uninstall_all

    selection = _resolve_selection(agents, use_all, list(UNINSTALLERS), "remove from")
    if not selection:
        print("nothing selected.")
        return
    try:
        results = uninstall_all(dry_run=dry_run, only=selection)
    except ValueError as e:
        sys.exit(str(e))
    width = max(len(n) for n in results)
    for name, outcome in results.items():
        mark = "-" if outcome.startswith("not present") else "+"
        print(f"{mark} {name:<{width}}  {outcome}")


def cmd_service_install(dry_run: bool) -> None:
    from .service import install

    results = install(dry_run=dry_run)
    for name, outcome in results.items():
        print(f"{name}: {outcome}")


def cmd_service_status() -> None:
    from .service import status

    for name, present in status().items():
        print(f"{name}: {'installed' if present else 'not installed'}")


def cmd_service_uninstall(dry_run: bool) -> None:
    from .service import uninstall

    for name, outcome in uninstall(dry_run=dry_run).items():
        print(f"{name}: {outcome}")


def cmd_doctor() -> None:
    from .doctor import run_doctor

    sys.exit(run_doctor())


def cmd_refresh() -> None:
    tokens = load_tokens()
    if not tokens:
        sys.exit("No tokens. Run: groken login")
    fresh = refresh_tokens(str(tokens["refreshToken"]))
    print("refreshed" if fresh else "refresh failed")


def cmd_agents() -> None:
    mgr = _manager()
    for a in mgr.command("listAgents"):
        print(f"{a['id']}  {a.get('name', '?')}  running={a.get('isRunning')}")


def cmd_bot_add(name: str) -> None:
    agent = _manager().create_bot(name)
    print(f"{agent['id']}  {agent.get('name', name.strip())}")


def cmd_bot_duplicate(source_name: str, name: str) -> None:
    agent = _manager().duplicate_bot(source_name, name)
    print(f"{agent['id']}  {agent.get('name', name.strip())}")


def cmd_team_create(name: str, bots: str, description: str) -> None:
    from .native_teams import NativeTeamError, NativeTeamGateway, create_native_team

    gateway = cast("NativeTeamGateway", cast(object, _manager()))
    try:
        team = create_native_team(gateway, name, bots.split(","), description)
    except NativeTeamError as exc:
        sys.exit(f"team: {exc}")
    print(f"Created native team {team.name} ({team.team_id})")


def cmd_team_members(name: str) -> None:
    from .native_teams import NativeTeamError, NativeTeamGateway, get_native_team

    gateway = cast("NativeTeamGateway", cast(object, _manager()))
    try:
        team = get_native_team(gateway, name)
    except NativeTeamError as exc:
        sys.exit(f"team: {exc}")
    for member in team.members:
        print(f"{member.agent_id}  {member.name}")


def cmd_team_ask(name: str, text: str, timeout: float) -> None:
    from .native_teams import NativeTeamError, NativeTeamGateway, ask_native_team

    gateway = cast("NativeTeamGateway", cast(object, _manager()))
    try:
        print(ask_native_team(gateway, name, text, timeout))
    except NativeTeamError as exc:
        sys.exit(f"team: {exc}")


def cmd_bot_env_capture(bot: str | None) -> str:
    from .env_manifest import capture_for_gateway

    outcome = capture_for_gateway(_manager(), bot)
    return f"source={outcome.source.value} manifest_id={outcome.manifest_id} path={outcome.local_path}"


def cmd_bot_env_restore(bot: str | None, *, yes: bool, retry_manual: bool) -> None:
    from .env_restore_gateway import RestoreCommandOptions, run_cli_restore

    run_cli_restore(_manager(), RestoreCommandOptions(bot, yes, retry_manual))


def cmd_bot_update(bot: str | None, *, yes: bool, skip_capture: bool) -> None:
    from .bot_update import UpdateOptions, run_cli_update

    run_cli_update(_manager(), UpdateOptions(bot, yes, skip_capture))


def cmd_list_bots() -> None:
    from .config import bot_name, cached_bot_id

    agents = list(_manager().command("listAgents"))
    if not agents:
        print("No Bots found.")
        return
    configured_name = bot_name()
    cached = cached_bot_id()
    configured_id = next(
        (
            str(agent["id"])
            for agent in agents
            if agent.get("id") == cached and agent.get("name") == configured_name
        ),
        None,
    )
    if configured_id is None:
        configured_id = next(
            (
                str(agent["id"])
                for agent in agents
                if agent.get("name") == configured_name
            ),
            None,
        )
    id_width = max(len(str(agent.get("id", "?"))) for agent in agents)
    name_width = max(len(str(agent.get("name", "?"))) for agent in agents)
    for agent in agents:
        bot_id = str(agent.get("id", "?"))
        name = str(agent.get("name", "?"))
        marker = "*" if bot_id == configured_id else " "
        print(
            f"{marker} {bot_id:<{id_width}}  {name:<{name_width}}  running={agent.get('isRunning')}"
        )


def cmd_gsend(agent_id: str | None, text: str) -> None:
    mgr = _manager()
    resp = mgr.send_prompt(mgr.resolve_agent(agent_id), text)
    print(json.dumps(resp))


def cmd_tail(
    agent_id: str | None,
    limit: int = 15,
    as_json: bool = False,
    since: str | None = None,
    full: bool = False,
) -> None:
    mgr = _manager()
    entries = mgr.transcript_tail(mgr.resolve_agent(agent_id))
    if since is not None:
        for index, entry in enumerate(entries):
            if entry.get("id") == since:
                entries = entries[index + 1 :]
                break
    entries = entries[-limit:] if limit else []
    structured: list[dict[str, object]] = []
    for entry in entries:
        message = entry.get("message")
        nested_content: object | None = None
        if isinstance(message, dict):
            nested_content = cast("dict[str, object]", message).get("content")
        structured.append(
            {
                "id": entry.get("id"),
                "kind": entry.get("kind"),
                "timestampMs": entry.get("timestampMs"),
                "content": nested_content or entry.get("content") or "",
            }
        )
    if as_json:
        print(json.dumps(structured, ensure_ascii=False))
        return
    for e in structured:
        content = e["content"] if full else str(e["content"])[:160]
        print(f"[{e['timestampMs']}] [{e['kind']}] {content}")


def cmd_gask(
    agent_id: str | None, text: str, timeout: float, stream: bool = False
) -> None:
    mgr = _manager()
    resolved = mgr.resolve_agent(agent_id)
    if not stream or not sys.stdout.isatty() or not hasattr(mgr, "ask_stream"):
        print(mgr.ask(resolved, text, timeout_s=timeout))
        return

    def emit(chunk: str) -> None:
        _ = sys.stdout.write(chunk)
        _ = sys.stdout.flush()

    _ = mgr.ask_stream(resolved, text, timeout_s=timeout, on_chunk=emit)


def cmd_events() -> None:
    mgr = _manager()
    for ev in mgr.events():
        print(json.dumps(ev, ensure_ascii=False)[:300])


def cmd_swarm_send(args: _CliArgs) -> None:
    from .swarm import SwarmRequest, SwarmSelectionError, render, run_swarm
    from .swarm_process import SubprocessRoundExecutor

    bots = None if args.swarm_bots is None else args.swarm_bots.split(",")
    exclude = () if args.swarm_exclude is None else args.swarm_exclude.split(",")
    try:
        outcome = run_swarm(
            _manager(),
            SwarmRequest(
                bots,
                args.text,
                exclude,
                args.swarm_timeout_s,
                args.swarm_rounds,
            ),
            SubprocessRoundExecutor(),
        )
    except SwarmSelectionError as exc:
        print(f"swarm: {exc}", file=sys.stderr)
        sys.exit(1)
    print(render(outcome))
    sys.exit(outcome.exit_code)


def cmd_swarm_rooms() -> None:
    from .swarm import SwarmError, read_rooms, render_rooms

    try:
        rooms = read_rooms(_manager())
    except SwarmError as exc:
        print(f"swarm: {exc}", file=sys.stderr)
        sys.exit(1)
    print(render_rooms(rooms))


def cmd_sandboxes() -> None:
    client = SandClient()
    print(json.dumps(client.list_sandboxes(), indent=2)[:4000])


def cmd_exec(command: str, cwd: str, timeout_ms: int) -> None:
    from .share_client import (
        RelayManager,
        ShareProtocolError,
        ShareRemoteError,
        load_share_link,
    )

    if not command.strip():
        print("exec: command must not be empty", file=sys.stderr)
        sys.exit(1)
    try:
        link = load_share_link()
        if link is not None:
            result = RelayManager(link).execute(command, cwd, timeout_ms)
        else:
            result = asyncio.run(ExecServiceClient().execute(command, cwd, timeout_ms))
    except (ExecServiceError, ShareProtocolError, ShareRemoteError) as exc:
        print(f"exec: {exc}", file=sys.stderr)
        sys.exit(1)
    _ = sys.stdout.write(result.stdout)
    _ = sys.stderr.write(result.stderr)
    sys.exit(result.exit_code)


def cmd_tools_list(server_identifiers: list[str], as_json: bool) -> None:
    from .plugin_tools import render_tool_catalog

    payload = SandClient().list_sand_mcp_tools(server_identifiers)
    print(
        json.dumps(payload, ensure_ascii=False, indent=2)
        if as_json
        else render_tool_catalog(payload)
    )


def cmd_tools_call(
    server_identifier: str,
    tool_name: str,
    arguments_json: str,
    bot: str | None,
    *,
    yes: bool,
) -> None:
    from .plugin_tools import parse_arguments_json, resolve_catalog_tool_name

    try:
        arguments = parse_arguments_json(arguments_json)
    except (TypeError, ValueError) as exc:
        sys.exit(f"tools call: {exc}")
    if not yes:
        if not sys.stdin.isatty():
            sys.exit("tools call requires --yes when not interactive")
        answer = (
            input(
                f"Execute {server_identifier}/{tool_name} with the connected account? [y/N] "
            )
            .strip()
            .lower()
        )
        if answer not in {"y", "yes"}:
            sys.exit("tools call cancelled")
    manager = _manager()
    client = SandClient()
    try:
        canonical_name = resolve_catalog_tool_name(
            client.list_sand_mcp_tools([server_identifier]),
            tool_name,
            server_identifier,
        )
    except (TypeError, ValueError) as exc:
        sys.exit(f"tools call: {exc}")
    result = client.execute_sand_mcp_tool(
        server_identifier=server_identifier,
        tool_name=canonical_name,
        arguments=arguments,
        tool_call_id=str(uuid.uuid4()),
        agent_id=manager.resolve_agent(bot),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_status(as_json: bool) -> None:
    from .status import collect_status, render_status

    status = collect_status(_manager())
    print(
        json.dumps(status, ensure_ascii=False, indent=2)
        if as_json
        else render_status(status)
    )


def cmd_capabilities() -> None:
    from .capabilities import capability_manifest, live_read_only_status

    manager = _manager()
    payload = capability_manifest()
    payload["live"] = live_read_only_status(manager)
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def cmd_inspect_app(app_path: str | None, fail_on_drift: bool) -> None:
    from .inspect_app import AsarError, inspect_app

    try:
        report = inspect_app(app_path)
    except AsarError as e:
        sys.exit(f"inspect-app: {e}")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if fail_on_drift and not report["drift"]["clean"]:
        sys.exit(2)


def cmd_routine_list() -> None:
    names = {routine.name for routine in list_routines()}
    names.update(template.name for template in BUILTIN_TEMPLATES)
    for routine_name in sorted(names):
        print(routine_name)


def cmd_routine_new(name: str) -> None:
    print(new_routine(name).entry)


def cmd_routine_edit(name: str) -> None:
    path = edit_path(name)
    editor = os.environ.get("EDITOR", "")
    if not editor:
        print(path)
        return
    try:
        editor_argv = shlex.split(editor)
    except ValueError as exc:
        print(f"routine edit: invalid EDITOR: {exc}", file=sys.stderr)
        sys.exit(1)
    if not editor_argv:
        print(path)
        return
    result = subprocess.run([*editor_argv, str(path)], check=False)
    if result.returncode:
        sys.exit(result.returncode)


def cmd_routine_run(name: str, event: RoutineEvent) -> None:
    sys.exit(run_routine(name, event))


def cmd_share_create(
    name: str, bot: str, manager: _BotRosterManager | None = None
) -> None:
    from .gateway import GatewayManager
    from .share_store import DuplicateShareError, ShareStore, ShareStoreDataError

    owner = manager or cast("_BotRosterManager", cast(object, GatewayManager()))
    agents_value = owner.command("listAgents")
    if not isinstance(agents_value, list):
        sys.exit("share create: invalid Bot roster")
    agents = [agent for agent in agents_value if isinstance(agent, dict)]
    matches = [
        agent for agent in agents if agent.get("id") == bot or agent.get("name") == bot
    ]
    if len(matches) != 1:
        reason = "ambiguous" if matches else "unknown"
        sys.exit(f"share create: {reason} Bot: {bot}")
    bot_id = matches[0].get("id")
    bot_name = matches[0].get("name")
    if not isinstance(bot_id, str) or not isinstance(bot_name, str):
        sys.exit("share create: Bot has invalid identity metadata")
    try:
        grant = ShareStore().create(name, bot_id, bot_name)
    except (DuplicateShareError, ShareStoreDataError) as exc:
        sys.exit(str(exc))
    print(
        f"Created share {grant.record.name} for "
        f"{grant.record.bot_name} ({grant.record.bot_id}): {grant.token}"
    )


def cmd_share_list() -> None:
    from .share_store import ShareStore, ShareStoreDataError

    try:
        records = ShareStore().list()
    except ShareStoreDataError as exc:
        sys.exit(str(exc))
    for record in records:
        state = "revoked" if record.revoked else "active"
        print(f"{record.name}  {record.bot_name}  {record.bot_id}  {state}")


def cmd_share_revoke(name: str) -> None:
    from .share_store import ShareNotFoundError, ShareStore, ShareStoreDataError

    try:
        _ = ShareStore().revoke(name)
    except (ShareNotFoundError, ShareStoreDataError) as exc:
        sys.exit(str(exc))
    print(f"Revoked share: {name}")


def cmd_share_serve(host: str, port: int) -> None:
    try:
        import uvicorn

        from .share_server import create_share_app
    except ModuleNotFoundError as exc:
        if exc.name not in {"anyio", "fastapi", "pydantic", "uvicorn"}:
            raise
        sys.exit(
            "share serve dependencies are missing; "
            "install with: uv pip install -e '.[share]'"
        )

    from .gateway import GatewayManager
    from .share_store import ShareStore

    uvicorn.run(create_share_app(GatewayManager, ShareStore()), host=host, port=port)


def cmd_share_connect(url: str, token_file: str | None = None) -> None:
    from .share_client import ShareLink, ShareLinkError, save_share_link

    try:
        if token_file is not None:
            token_path = Path(token_file)
            if token_path.stat().st_mode & 0o077:
                sys.exit("share connect: token file must have mode 0600")
            token = token_path.read_text().strip()
        elif sys.stdin.isatty():
            token = getpass.getpass("Share token: ").strip()
        else:
            token = sys.stdin.readline().strip()
    except OSError:
        sys.exit("share connect: unable to read token file")
    try:
        link = ShareLink(url, token)
    except ShareLinkError as exc:
        sys.exit(f"share connect: {exc}")
    save_share_link(link)
    print(f"Connected to share relay: {link.url}")


def cmd_share_disconnect() -> None:
    from .share_client import clear_share_link

    print("Disconnected." if clear_share_link() else "No share connection.")


def cmd_share_status() -> None:
    from .share_client import load_share_link

    link = load_share_link()
    print(
        f"Connected to share relay: {link.url}"
        if link
        else "Not connected to a share relay."
    )


def _main_impl() -> None:
    p = argparse.ArgumentParser(
        prog="groken",
        description="Use Grok Bot cloud computers from a terminal or AI-agent harness.",
    )
    import importlib.metadata

    _ = p.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {importlib.metadata.version('groken')}",
    )
    sub = p.add_subparsers(dest="cmd", required=False)
    _ = sub.add_parser("guide", help="show the first-run and everyday command guide")
    _ = sub.add_parser("login", help="sign in with the account that owns Grok Bot")
    _ = sub.add_parser("refresh", help="refresh the saved account tokens")
    _ = sub.add_parser("doctor", help="check login, gateway, services, and integrations")
    sp = sub.add_parser(
        "install",
        help="connect groken to detected AI-agent harnesses",
        description="Connect groken to selected AI-agent harnesses.", 
        epilog=(
            "targets: claude-code, claude-desktop, claude-skills, codex, "
            "codex-skills, cursor, cursor-skills, vscode, windsurf, "
            "gemini-cli, opencode, kiro, hermes, gjc, gjc-skills, openclaw, omo, "
            "omo-skill"
        ),
    )
    _ = sp.add_argument("agents", nargs="*")
    _ = sp.add_argument("--all", action="store_true", dest="use_all")
    _ = sp.add_argument("--dry-run", action="store_true")
    sp = sub.add_parser("uninstall")
    _ = sp.add_argument("agents", nargs="*")
    _ = sp.add_argument("--all", action="store_true", dest="use_all")
    _ = sp.add_argument("--dry-run", action="store_true")
    _ = sub.add_parser("bots")
    _ = sub.add_parser("agents")
    bot_parser = sub.add_parser("bot", help="manage Bots")
    bot_sub = bot_parser.add_subparsers(dest="bot_cmd", required=True)
    bot_add = bot_sub.add_parser("add", help="create a Bot")
    _ = bot_add.add_argument("bot_name", metavar="NAME")
    bot_duplicate = bot_sub.add_parser("duplicate", help="duplicate a Bot")
    _ = bot_duplicate.add_argument("source_bot", metavar="SOURCE")
    _ = bot_duplicate.add_argument("bot_name", metavar="NEW")
    bot_env = bot_sub.add_parser("env", help="capture or restore a Bot environment")
    bot_env_sub = bot_env.add_subparsers(dest="bot_env_cmd", required=True)
    bot_env_capture = bot_env_sub.add_parser(
        "capture", help="capture package and app inventory"
    )
    _ = bot_env_capture.add_argument("env_bot", metavar="BOT", nargs="?")
    bot_env_restore = bot_env_sub.add_parser(
        "restore", help="restore missing package and app inventory"
    )
    _ = bot_env_restore.add_argument("env_bot", metavar="BOT", nargs="?")
    _ = bot_env_restore.add_argument(
        "--yes", action="store_true", help="restore without prompting"
    )
    _ = bot_env_restore.add_argument(
        "--retry-manual",
        action="store_true",
        help="retry manual-action operations once",
    )
    bot_update = bot_sub.add_parser(
        "update", help="manually update a Bot and preserve its environment"
    )
    _ = bot_update.add_argument("update_bot", metavar="BOT", nargs="?")
    _ = bot_update.add_argument(
        "--yes", action="store_true", help="restore without prompting"
    )
    _ = bot_update.add_argument(
        "--skip-capture", action="store_true", help="require a fresh existing manifest"
    )
    share_parser = sub.add_parser("share", help="create and use shared relay links")
    share_sub = share_parser.add_subparsers(dest="share_cmd", required=True)
    share_create = share_sub.add_parser("create")
    _ = share_create.add_argument("--name", dest="share_name", required=True)
    _ = share_create.add_argument("--bot", dest="share_bot", required=True)
    _ = share_sub.add_parser("list")
    share_revoke = share_sub.add_parser("revoke")
    _ = share_revoke.add_argument("share_name", metavar="NAME")
    share_serve = share_sub.add_parser("serve")
    _ = share_serve.add_argument("--host", dest="share_host", default="127.0.0.1")
    _ = share_serve.add_argument("--port", dest="share_port", type=int, default=8787)
    share_connect = share_sub.add_parser("connect")
    _ = share_connect.add_argument("share_url", metavar="URL")
    _ = share_connect.add_argument(
        "--token-file", dest="share_token_file", metavar="PATH"
    )
    _ = share_sub.add_parser("disconnect")
    _ = share_sub.add_parser("status")
    team_parser = sub.add_parser("team", help="manage native Grok Bot teams")
    team_sub = team_parser.add_subparsers(dest="team_cmd", required=True)
    team_create = team_sub.add_parser("create", help="create a persistent Bot group")
    _ = team_create.add_argument("team_name", metavar="NAME")
    _ = team_create.add_argument(
        "--bots",
        dest="team_bots",
        required=True,
        help="comma-separated Bot names or ids",
    )
    _ = team_create.add_argument("--description", dest="team_description", default="")
    team_members = team_sub.add_parser("members", help="list native team members")
    _ = team_members.add_argument("team_name", metavar="TEAM")
    team_ask = team_sub.add_parser("ask", help="message one native team")
    _ = team_ask.add_argument("team_name", metavar="TEAM")
    _ = team_ask.add_argument("text")
    _ = team_ask.add_argument("--timeout", type=float, default=600)
    configure_parser = sub.add_parser(
        "configure", help="choose this machine's default Bot"
    )
    _ = configure_parser.add_argument(
        "bot", nargs="?", help="Bot name or id; omit for an interactive menu"
    )
    _ = sub.add_parser(
        "list", help="list Bots and mark this machine's configured default"
    )
    connect_parser = sub.add_parser(
        "connect",
        help="open the configured or named Bot's computer",
        description="Open a Bot's cloud computer in the browser after it is ready.",
        epilog="example: groken connect my-bot",
    )
    _ = connect_parser.add_argument(
        "bot", nargs="?", help="Bot name or id; defaults to the configured Bot"
    )
    _ = connect_parser.add_argument(
        "--display", type=int, default=None, help="explicit display override"
    )
    tools_parser = sub.add_parser(
        "tools", help="discover and execute connected plugin tools"
    )
    tools_sub = tools_parser.add_subparsers(dest="tools_cmd", required=True)
    tools_list = tools_sub.add_parser("list", help="list connected plugin tools")
    _ = tools_list.add_argument(
        "servers", nargs="*", help="optional server identifiers"
    )
    _ = tools_list.add_argument("--json", action="store_true", dest="as_json")
    tools_call = tools_sub.add_parser("call", help="execute one plugin tool")
    _ = tools_call.add_argument("server", help="server identifier, for example user-X")
    _ = tools_call.add_argument("tool", help="backend tool name")
    _ = tools_call.add_argument(
        "--args-json", default="{}", help="tool arguments as a JSON object"
    )
    _ = tools_call.add_argument(
        "--bot", default=None, help="Bot name or id used for audit attribution"
    )
    _ = tools_call.add_argument(
        "--yes", action="store_true", help="confirm direct execution"
    )
    status_parser = sub.add_parser(
        "status", help="show Bot, host, secrets, MCP, and local health"
    )
    _ = status_parser.add_argument("--json", action="store_true", dest="as_json")
    _ = sub.add_parser(
        "capabilities", help="show the audited gateway inventory and safe live status"
    )
    vnc_parser = sub.add_parser(
        "vnc", help="open the default Bot computer (connect is the simpler alias)"
    )
    _ = vnc_parser.add_argument(
        "--open",
        action="store_true",
        dest="open_browser",
        help="deprecated no-op; vnc always waits, then opens the browser",
    )
    _ = vnc_parser.add_argument(
        "--display",
        type=int,
        default=None,
        help="Grok display number; defaults to the configured bot's computer",
    )
    _ = sub.add_parser("events", help="stream raw gateway events")
    sp = sub.add_parser("send", help="send a task without waiting for its reply")
    _ = sp.add_argument("text", help="task text")
    _ = sp.add_argument("agent", nargs="?", help="Bot name or id; defaults locally")
    sp = sub.add_parser("tail", help="show recent conversation entries")
    _ = sp.add_argument("agent", nargs="?")
    _ = sp.add_argument("-n", "--limit", type=int, default=15)
    _ = sp.add_argument("--json", action="store_true", dest="as_json")
    _ = sp.add_argument("--since")
    _ = sp.add_argument("--full", action="store_true")
    sp = sub.add_parser(
        "ask",
        help="send a task and wait for the reply",
        description="Send a task to a Bot and wait for its reply using SSE when available.",
        epilog='example: groken ask "Review this repository and verify the result"',
    )
    _ = sp.add_argument("text", help="task text")
    _ = sp.add_argument("agent", nargs="?", help="Bot name or id; defaults locally")
    _ = sp.add_argument("--timeout", type=float, default=600, help="seconds to wait")
    _ = sp.add_argument("--stream", action="store_true", help="stream reply chunks")
    swarm_parser = sub.add_parser(
        "swarm", help="ask existing Bots concurrently or inspect shared rooms"
    )
    swarm_sub = swarm_parser.add_subparsers(dest="swarm_cmd", required=True)
    swarm_send = swarm_sub.add_parser("send", help="ask existing Bots concurrently")
    _ = swarm_send.add_argument("text", help="task sent to every selected Bot")
    _ = swarm_send.add_argument(
        "--bots", dest="swarm_bots", help="comma-separated Bot names or ids"
    )
    _ = swarm_send.add_argument(
        "--exclude", dest="swarm_exclude", help="comma-separated Bots to omit"
    )
    _ = swarm_send.add_argument(
        "--timeout-s", dest="swarm_timeout_s", type=float, default=600
    )
    _ = swarm_send.add_argument("--rounds", dest="swarm_rounds", type=int, default=1)
    _ = swarm_sub.add_parser(
        "rooms", help="read shared-room state without creating, joining, or leaving"
    )
    _ = sub.add_parser("sandboxes")
    sp = sub.add_parser("exec")
    _ = sp.add_argument("command")
    _ = sp.add_argument("--cwd", default="/workspace")
    _ = sp.add_argument("--timeout-ms", type=int, default=15000)
    service_parser = sub.add_parser("service", help="manage groken launchd services")
    service_sub = service_parser.add_subparsers(dest="service_cmd", required=True)
    service_install = service_sub.add_parser("install")
    _ = service_install.add_argument("--dry-run", action="store_true")
    _ = service_sub.add_parser("status")
    service_uninstall = service_sub.add_parser("uninstall")
    _ = service_uninstall.add_argument("--dry-run", action="store_true")
    routine_parser = sub.add_parser("routine", help="manage local routines")
    routine_sub = routine_parser.add_subparsers(dest="routine_cmd", required=True)
    _ = routine_sub.add_parser("list", help="list routines and built-in templates")
    for routine_command in ("new", "edit"):
        routine_action = routine_sub.add_parser(routine_command)
        _ = routine_action.add_argument("routine_name", metavar="NAME")
    routine_run = routine_sub.add_parser("run")
    _ = routine_run.add_argument("routine_name", metavar="NAME")
    _ = routine_run.add_argument(
        "--event",
        dest="routine_event",
        type=RoutineEvent,
        choices=tuple(RoutineEvent),
        default=RoutineEvent.MANUAL,
    )
    sp = sub.add_parser(
        "inspect-app", help="compare the installed Grok Bot app with audited contracts"
    )
    _ = sp.add_argument("--app-path", default=None)
    _ = sp.add_argument("--fail-on-drift", action="store_true")
    args = p.parse_args(namespace=_CliArgs())
    if args.cmd is None or args.cmd == "guide":
        print(GUIDE)
        return
    _enforce_share_mode(args)
    {
        "login": cmd_login,
        "refresh": cmd_refresh,
        "share": lambda: {
            "create": lambda: cmd_share_create(args.share_name, args.share_bot),
            "list": cmd_share_list,
            "revoke": lambda: cmd_share_revoke(args.share_name),
            "serve": lambda: cmd_share_serve(args.share_host, args.share_port),
            "connect": lambda: cmd_share_connect(args.share_url, args.share_token_file),
            "disconnect": cmd_share_disconnect,
            "status": cmd_share_status,
        }[args.share_cmd or "status"](),
        "doctor": cmd_doctor,
        "install": lambda: cmd_install(args.agents, args.dry_run, args.use_all),
        "uninstall": lambda: cmd_uninstall(args.agents, args.dry_run, args.use_all),
        "bots": cmd_agents,
        "agents": cmd_agents,
        "bot": lambda: {
            "add": lambda: cmd_bot_add(args.bot_name),
            "duplicate": lambda: cmd_bot_duplicate(args.source_bot, args.bot_name),
            "env": lambda: {
                "capture": lambda: print(cmd_bot_env_capture(args.env_bot)),
                "restore": lambda: cmd_bot_env_restore(
                    args.env_bot, yes=args.yes, retry_manual=args.retry_manual
                ),
            }[args.bot_env_cmd or "capture"](),
            "update": lambda: cmd_bot_update(
                args.update_bot, yes=args.yes, skip_capture=args.skip_capture
            ),
        }[args.bot_cmd or "add"](),
        "team": lambda: {
            "create": lambda: cmd_team_create(
                args.team_name, args.team_bots, args.team_description
            ),
            "members": lambda: cmd_team_members(args.team_name),
            "ask": lambda: cmd_team_ask(args.team_name, args.text, args.timeout),
        }[args.team_cmd or "members"](),
        "configure": lambda: cmd_configure(args.bot),
        "list": cmd_list_bots,
        "connect": lambda: cmd_vnc(False, args.display, args.bot),
        "tools": lambda: {
            "list": lambda: cmd_tools_list(args.servers, args.as_json),
            "call": lambda: cmd_tools_call(
                args.server,
                args.tool,
                args.args_json,
                args.bot,
                yes=args.yes,
            ),
        }[args.tools_cmd](),
        "status": lambda: cmd_status(args.as_json),
        "capabilities": cmd_capabilities,
        "vnc": lambda: cmd_vnc(args.open_browser, args.display),
        "events": cmd_events,
        "send": lambda: cmd_gsend(args.agent, args.text),
        "tail": lambda: cmd_tail(
            args.agent, args.limit, args.as_json, args.since, args.full
        ),
        "ask": lambda: cmd_gask(args.agent, args.text, args.timeout, args.stream),
        "swarm": lambda: {
            "send": lambda: cmd_swarm_send(args),
            "rooms": cmd_swarm_rooms,
        }[args.swarm_cmd or "rooms"](),
        "sandboxes": cmd_sandboxes,
        "exec": lambda: cmd_exec(args.command, args.cwd, args.timeout_ms),
        "service": lambda: {
            "install": lambda: cmd_service_install(args.dry_run),
            "status": cmd_service_status,
            "uninstall": lambda: cmd_service_uninstall(args.dry_run),
        }[args.service_cmd](),
        "routine": lambda: {
            "list": cmd_routine_list,
            "new": lambda: cmd_routine_new(args.routine_name),
            "edit": lambda: cmd_routine_edit(args.routine_name),
            "run": lambda: cmd_routine_run(args.routine_name, args.routine_event),
        }[args.routine_cmd or "list"](),
        "inspect-app": lambda: cmd_inspect_app(args.app_path, args.fail_on_drift),
    }[args.cmd]()


def main() -> None:
    from .auth import TokenStateError
    from .bot_update import BotUpdateError
    from .client import ConnectError
    from .config import ConfigStateError
    from .env_manifest import CaptureError
    from .env_restore_manifest import RestoreManifestError
    from .env_restore_run import RestorePendingError
    from .errors import explain_error
    from .share_client import SharePermissionError, ShareProtocolError, ShareRemoteError

    try:
        _main_impl()
    except ConnectError as e:
        print(explain_error(e), file=sys.stderr)
        sys.exit(1)
    except RoutineError as e:
        print(f"routine: {e}", file=sys.stderr)
        sys.exit(1)
    except CaptureError as e:
        print(f"env capture: {e}", file=sys.stderr)
        sys.exit(1)
    except RestoreManifestError as e:
        print(f"env restore: {e}", file=sys.stderr)
        sys.exit(1)
    except RestorePendingError as e:
        print(f"env restore: {e}", file=sys.stderr)
        sys.exit(1)
    except BotUpdateError as e:
        print(f"bot update: {e}", file=sys.stderr)
        sys.exit(1)
    except (SharePermissionError, ShareProtocolError, ShareRemoteError) as e:
        print(f"share: {e}", file=sys.stderr)
        sys.exit(1)
    except (TokenStateError, ConfigStateError) as e:
        print(e, file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
