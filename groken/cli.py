import argparse
import asyncio
import json
import sys
import threading
import uuid
import webbrowser

from .auth import load_tokens, poll_for_tokens, refresh_tokens, start_login
from .client import SandClient
from .exec_service import ExecServiceClient, ExecServiceError


def _manager():
    from .gateway import GatewayManager
    return GatewayManager()


def cmd_login() -> None:
    params = start_login()
    print("Open this URL and sign in with the account that owns Grok Bot:\n")
    print(params["login_url"], "\n")
    try:
        webbrowser.open(params["login_url"])
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
  groken install            pick which AI agents get the groken MCP server
  groken doctor             verify tokens, sandbox, and your dedicated Bot

Everyday use:
  groken configure          choose this machine's default Bot
  groken list               list Bots (* marks the configured Bot)
  groken connect [bot]      open the configured or named Bot's computer
  groken ask "task"         send a task, wait for the reply
  groken send "task"        fire-and-forget
  groken tail               recent conversation
  groken capabilities       official gateway inventory + safe live status
  groken inspect-app        diff the installed app's command table vs groken

Run any command with --help for its options."""


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


def _resolve_selection(agents: list[str], use_all: bool, candidates: list[str], action: str) -> list[str]:
    if agents:
        return agents
    if use_all:
        return candidates
    if not sys.stdin.isatty():
        sys.exit(f"no agents selected. Pass agent names, or --all. Detected: {', '.join(candidates) or 'none'}")
    if not candidates:
        sys.exit("no supported AI agents detected on this machine.")
    return _select_agents(candidates, action)


def _find_bot(agents: list[dict[str, object]], selector: str) -> dict[str, object] | None:
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

    agents = list(_manager().command("listAgents"))
    if not agents:
        sys.exit("configure: no Bots found")
    selected: dict[str, object] | None = None
    attempted = bot
    if bot is not None:
        selected = _find_bot(agents, bot)
    elif not sys.stdin.isatty():
        sys.exit("configure: pass a Bot name or id when not interactive")
    else:
        print("Choose the default Bot for this machine:\n")
        for index, agent in enumerate(agents, 1):
            print(f'  {index}. {agent.get("name", "?")}  ({agent.get("id", "?")})')
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
    print(f"Configured Bot: {name} ({bot_id})")


def cmd_vnc(
    _open_browser: bool,
    display: int | None = None,
    bot: str | None = None,
) -> None:
    from .vnc import display_from_forever_box, vnc_url
    from .vnc_proxy import serve_vnc_proxy
    from .vnc_ready import VncNotReadyError, wait_until_vnc_ready
    manager = _manager()
    bot_id = manager.own_agent_id() if bot is None else manager.resolve_agent(bot)
    selected_display = display
    if selected_display is None:
        status = manager.command("getForeverBoxStatus", {"id": bot_id})
        selected_display = display_from_forever_box(status)
        if selected_display is None:
            status = manager.command("ensureForeverBox", {"id": bot_id})
            selected_display = display_from_forever_box(status)
        if selected_display is None:
            sys.exit("vnc: configured bot has no available computer display")
    try:
        url = vnc_url(manager.ensure_sandbox_metadata(), display=selected_display)
    except ValueError as exc:
        sys.exit(f"vnc: {exc}")
    try:
        session = serve_vnc_proxy(url)
    except ValueError as exc:
        sys.exit(f"vnc: {exc}")
    label = "configured bot" if bot is None else "bot"
    print(f"Using {label} {bot_id} display :{selected_display}.", file=sys.stderr)
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
        webbrowser.open(session.local_url)
    except (webbrowser.Error, OSError):
        pass
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        session.close()


def cmd_install(agents: list[str], dry_run: bool, use_all: bool) -> None:
    from .config import load_config, set_vnc_enabled
    from .installers import detected_agents, install_all
    if not dry_run and "vnc" not in load_config() and sys.stdin.isatty():
        answer = input("Enable groken vnc? [y/N] ").strip().lower()
        set_vnc_enabled(answer in {"y", "yes"})
    selection = _resolve_selection(agents, use_all, detected_agents(), "install into")
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
        print(f'{a["id"]}  {a.get("name", "?")}  running={a.get("isRunning")}')


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
            f"{marker} {bot_id:<{id_width}}  {name:<{name_width}}  "
            f"running={agent.get('isRunning')}"
        )


def cmd_gsend(agent_id: str | None, text: str) -> None:
    mgr = _manager()
    resp = mgr.send_prompt(mgr.resolve_agent(agent_id), text)
    print(json.dumps(resp))


def cmd_tail(agent_id: str | None, limit: int = 15, as_json: bool = False, since: str | None = None, full: bool = False) -> None:
    mgr = _manager()
    entries = mgr.transcript_tail(mgr.resolve_agent(agent_id))
    if since is not None:
        for index, entry in enumerate(entries):
            if entry.get("id") == since:
                entries = entries[index + 1:]
                break
    entries = entries[-limit:] if limit else []
    structured = [
        {"id": e.get("id"), "kind": e.get("kind"), "timestampMs": e.get("timestampMs"), "content": e.get("content") or ""}
        for e in entries
    ]
    if as_json:
        print(json.dumps(structured, ensure_ascii=False))
        return
    for e in structured:
        content = e["content"] if full else str(e["content"])[:160]
        print(f'[{e["timestampMs"]}] [{e["kind"]}] {content}')


def cmd_gask(agent_id: str | None, text: str, timeout: float, stream: bool = False) -> None:
    mgr = _manager()
    resolved = mgr.resolve_agent(agent_id)
    if not stream or not sys.stdout.isatty() or not hasattr(mgr, "ask_stream"):
        print(mgr.ask(resolved, text, timeout_s=timeout))
        return

    def emit(chunk: str) -> None:
        sys.stdout.write(chunk)
        sys.stdout.flush()

    mgr.ask_stream(resolved, text, timeout_s=timeout, on_chunk=emit)


def cmd_events() -> None:
    mgr = _manager()
    for ev in mgr.events():
        print(json.dumps(ev, ensure_ascii=False)[:300])


def cmd_sandboxes() -> None:
    client = SandClient()
    print(json.dumps(client.list_sandboxes(), indent=2)[:4000])


def cmd_exec(command: str, cwd: str, timeout_ms: int) -> None:
    if not command.strip():
        print("exec: command must not be empty", file=sys.stderr)
        sys.exit(1)
    try:
        result = asyncio.run(ExecServiceClient().execute(command, cwd, timeout_ms))
    except ExecServiceError as exc:
        print(f"exec: {exc}", file=sys.stderr)
        sys.exit(1)
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    sys.exit(1 if result.stderr else 0)


def cmd_tools_list(server_identifiers: list[str], as_json: bool) -> None:
    from .plugin_tools import render_tool_catalog

    payload = SandClient().list_sand_mcp_tools(server_identifiers)
    print(json.dumps(payload, ensure_ascii=False, indent=2) if as_json else render_tool_catalog(payload))


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
        answer = input(
            f"Execute {server_identifier}/{tool_name} with the connected account? [y/N] "
        ).strip().lower()
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
    print(json.dumps(status, ensure_ascii=False, indent=2) if as_json else render_status(status))


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


def _main_impl() -> None:
    p = argparse.ArgumentParser(prog="groken")
    import importlib.metadata
    p.add_argument("--version", action="version", version=f"%(prog)s {importlib.metadata.version('groken')}")
    sub = p.add_subparsers(dest="cmd", required=False)
    sub.add_parser("login")
    sub.add_parser("refresh")
    sub.add_parser("doctor")
    sp = sub.add_parser("install")
    sp.add_argument("agents", nargs="*")
    sp.add_argument("--all", action="store_true", dest="use_all")
    sp.add_argument("--dry-run", action="store_true")
    sp = sub.add_parser("uninstall")
    sp.add_argument("agents", nargs="*")
    sp.add_argument("--all", action="store_true", dest="use_all")
    sp.add_argument("--dry-run", action="store_true")
    sub.add_parser("bots")
    sub.add_parser("agents")
    configure_parser = sub.add_parser("configure", help="choose this machine's default Bot")
    configure_parser.add_argument("bot", nargs="?", help="Bot name or id; omit for an interactive menu")
    sub.add_parser("list", help="list Bots and mark this machine's configured default")
    connect_parser = sub.add_parser("connect", help="open the configured or named Bot's computer")
    connect_parser.add_argument("bot", nargs="?", help="Bot name or id; defaults to the configured Bot")
    connect_parser.add_argument("--display", type=int, default=None, help="explicit display override")
    tools_parser = sub.add_parser("tools", help="discover and execute connected plugin tools")
    tools_sub = tools_parser.add_subparsers(dest="tools_cmd", required=True)
    tools_list = tools_sub.add_parser("list", help="list connected plugin tools")
    tools_list.add_argument("servers", nargs="*", help="optional server identifiers")
    tools_list.add_argument("--json", action="store_true", dest="as_json")
    tools_call = tools_sub.add_parser("call", help="execute one plugin tool")
    tools_call.add_argument("server", help="server identifier, for example user-X")
    tools_call.add_argument("tool", help="backend tool name")
    tools_call.add_argument("--args-json", default="{}", help="tool arguments as a JSON object")
    tools_call.add_argument("--bot", default=None, help="Bot name or id used for audit attribution")
    tools_call.add_argument("--yes", action="store_true", help="confirm direct execution")
    status_parser = sub.add_parser("status", help="show Bot, host, storage, and MCP health")
    status_parser.add_argument("--json", action="store_true", dest="as_json")
    sub.add_parser("capabilities")
    vnc_parser = sub.add_parser("vnc")
    vnc_parser.add_argument(
        "--open",
        action="store_true",
        dest="open_browser",
        help="ignored; vnc always waits for the desktop, then opens the browser",
    )
    vnc_parser.add_argument(
        "--display",
        type=int,
        default=None,
        help="Grok display number; defaults to the configured bot's computer",
    )
    sub.add_parser("events")
    sp = sub.add_parser("send"); sp.add_argument("text"); sp.add_argument("agent", nargs="?")
    sp = sub.add_parser("tail")
    sp.add_argument("agent", nargs="?")
    sp.add_argument("-n", "--limit", type=int, default=15)
    sp.add_argument("--json", action="store_true", dest="as_json")
    sp.add_argument("--since")
    sp.add_argument("--full", action="store_true")
    sp = sub.add_parser("ask"); sp.add_argument("text"); sp.add_argument("agent", nargs="?"); sp.add_argument("--timeout", type=float, default=600); sp.add_argument("--stream", action="store_true")
    sub.add_parser("sandboxes")
    sp = sub.add_parser("exec")
    sp.add_argument("command")
    sp.add_argument("--cwd", default="/workspace")
    sp.add_argument("--timeout-ms", type=int, default=15000)
    service_parser = sub.add_parser("service", help="manage groken1 launchd services")
    service_sub = service_parser.add_subparsers(dest="service_cmd", required=True)
    service_install = service_sub.add_parser("install")
    service_install.add_argument("--dry-run", action="store_true")
    service_sub.add_parser("status")
    service_uninstall = service_sub.add_parser("uninstall")
    service_uninstall.add_argument("--dry-run", action="store_true")
    sp = sub.add_parser("inspect-app")
    sp.add_argument("--app-path", default=None)
    sp.add_argument("--fail-on-drift", action="store_true")
    args = p.parse_args()
    if args.cmd is None:
        print(GUIDE)
        return
    {
        "login": cmd_login,
        "refresh": cmd_refresh,
        "doctor": cmd_doctor,
        "install": lambda: cmd_install(args.agents, args.dry_run, args.use_all),
        "uninstall": lambda: cmd_uninstall(args.agents, args.dry_run, args.use_all),
        "bots": cmd_agents,
        "agents": cmd_agents,
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
        "tail": lambda: cmd_tail(args.agent, args.limit, args.as_json, args.since, args.full),
        "ask": lambda: cmd_gask(args.agent, args.text, args.timeout, args.stream),
        "sandboxes": cmd_sandboxes,
        "exec": lambda: cmd_exec(args.command, args.cwd, args.timeout_ms),
        "service": lambda: {
            "install": lambda: cmd_service_install(args.dry_run),
            "status": cmd_service_status,
            "uninstall": lambda: cmd_service_uninstall(args.dry_run),
        }[args.service_cmd](),
        "inspect-app": lambda: cmd_inspect_app(args.app_path, args.fail_on_drift),
    }[args.cmd]()


def main() -> None:
    from .client import ConnectError
    from .errors import explain_error
    try:
        _main_impl()
    except ConnectError as e:
        print(explain_error(e), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
