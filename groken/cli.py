import argparse
import json
import sys
import webbrowser

from .auth import load_tokens, poll_for_tokens, refresh_tokens, start_login
from .client import SandClient


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
  groken ask "task"         send a task, wait for the reply
  groken send "task"        fire-and-forget
  groken tail               recent conversation
  groken agents             list your Bots
  groken capabilities       official gateway inventory + safe live status

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


def cmd_install(agents: list[str], dry_run: bool, use_all: bool) -> None:
    from .installers import detected_agents, install_all

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


def cmd_doctor() -> None:
    import importlib.metadata

    from .client import detect_client_version
    from .config import bot_name, cached_bot_id

    ok = True
    version = importlib.metadata.version("groken")
    print(f"groken {version} | app client version: {detect_client_version()}")
    tokens = load_tokens()
    if tokens and "accessToken" in tokens:
        print("tokens: present")
    else:
        ok = False
        print("tokens: MISSING — run: groken login")
    if tokens:
        try:
            mgr = _manager()
            box = mgr._ensure_sandbox()
            print(f"sandbox: pod {box.get('podId', '?')} ({'gateway ok' if box.get('gatewayUrl') else 'no gateway url'})")
            agents = mgr.command("listAgents")
            print(f"agents: {len(agents)} visible")
            own = mgr.own_agent_id()
            print(f"own bot ({bot_name()}): {own}" + (" (cached)" if cached_bot_id() == own else ""))
        except Exception as e:  # noqa: BLE001 - doctor reports all startup failures
            ok = False
            print(f"sandbox/agents: FAIL — {e}")
    sys.exit(0 if ok else 1)


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


def cmd_gask(agent_id: str | None, text: str, timeout: float) -> None:
    mgr = _manager()
    print(mgr.ask(mgr.resolve_agent(agent_id), text, timeout_s=timeout))


def cmd_events() -> None:
    mgr = _manager()
    for ev in mgr.events():
        print(json.dumps(ev, ensure_ascii=False)[:300])


def cmd_sandboxes() -> None:
    client = SandClient()
    print(json.dumps(client.list_sandboxes(), indent=2)[:4000])


def cmd_capabilities() -> None:
    from .capabilities import capability_manifest, live_read_only_status

    manager = _manager()
    payload = capability_manifest()
    payload["live"] = live_read_only_status(manager)
    print(json.dumps(payload, ensure_ascii=False, indent=2))



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
    sub.add_parser("capabilities")
    sub.add_parser("events")
    sp = sub.add_parser("send"); sp.add_argument("text"); sp.add_argument("agent", nargs="?")
    sp = sub.add_parser("tail")
    sp.add_argument("agent", nargs="?")
    sp.add_argument("-n", "--limit", type=int, default=15)
    sp.add_argument("--json", action="store_true", dest="as_json")
    sp.add_argument("--since")
    sp.add_argument("--full", action="store_true")
    sp = sub.add_parser("ask"); sp.add_argument("text"); sp.add_argument("agent", nargs="?"); sp.add_argument("--timeout", type=float, default=600)
    sub.add_parser("sandboxes")
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
        "capabilities": cmd_capabilities,
        "events": cmd_events,
        "send": lambda: cmd_gsend(args.agent, args.text),
        "tail": lambda: cmd_tail(args.agent, args.limit, args.as_json, args.since, args.full),
        "ask": lambda: cmd_gask(args.agent, args.text, args.timeout),
        "sandboxes": cmd_sandboxes,
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
