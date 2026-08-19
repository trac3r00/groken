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


def cmd_install(agents: list[str] | None, dry_run: bool) -> None:
    from .installers import INSTALLERS, install_all

    try:
        results = install_all(dry_run=dry_run, only=agents or None)
    except ValueError as e:
        sys.exit(str(e))
    width = max(len(n) for n in INSTALLERS)
    for name, outcome in results.items():
        print(f"{name:<{width}}  {outcome}")


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
        except Exception as e:
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
    resp = mgr.session().send_prompt(mgr.resolve_agent(agent_id), text)
    print(json.dumps(resp))


def cmd_tail(agent_id: str | None) -> None:
    mgr = _manager()
    for e in mgr.session().transcript_tail(mgr.resolve_agent(agent_id))[-15:]:
        msg = e.get("message") or {}
        content = msg.get("content") or ""
        print(f'[{e.get("kind")}] {str(content)[:160]}')


def cmd_gask(agent_id: str | None, text: str, timeout: float) -> None:
    mgr = _manager()
    print(mgr.session().ask(mgr.resolve_agent(agent_id), text, timeout_s=timeout))


def cmd_events() -> None:
    mgr = _manager()
    for ev in mgr.session().events():
        print(json.dumps(ev, ensure_ascii=False)[:300])


def cmd_sandboxes() -> None:
    client = SandClient()
    print(json.dumps(client.list_sandboxes(), indent=2)[:4000])


def _main_impl() -> None:
    p = argparse.ArgumentParser(prog="groken")
    import importlib.metadata
    p.add_argument("--version", action="version", version=f"%(prog)s {importlib.metadata.version('groken')}")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("login")
    sub.add_parser("refresh")
    sub.add_parser("doctor")
    sp = sub.add_parser("install")
    sp.add_argument("agents", nargs="*")
    sp.add_argument("--dry-run", action="store_true")
    sub.add_parser("bots")
    sub.add_parser("agents")
    sub.add_parser("events")
    sp = sub.add_parser("send"); sp.add_argument("text"); sp.add_argument("agent", nargs="?")
    sp = sub.add_parser("tail"); sp.add_argument("agent", nargs="?")
    sp = sub.add_parser("ask"); sp.add_argument("text"); sp.add_argument("agent", nargs="?"); sp.add_argument("--timeout", type=float, default=600)
    sub.add_parser("sandboxes")
    args = p.parse_args()
    {
        "login": cmd_login,
        "refresh": cmd_refresh,
        "doctor": cmd_doctor,
        "install": lambda: cmd_install(args.agents, args.dry_run),
        "bots": cmd_agents,
        "agents": cmd_agents,
        "events": cmd_events,
        "send": lambda: cmd_gsend(args.agent, args.text),
        "tail": lambda: cmd_tail(args.agent),
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
