# groken — Project Knowledge Base

## OVERVIEW
macOS-only Python bridge that lets agents (omo/Hermes) chat with Grok Bot cloud agents in real time: Cursor OAuth PKCE login → `EnsureSandBox` (Connect-RPC) → per-pod gateway (`POST /api/<command>` + `/events` SSE). Ships as `groken` CLI + `groken-mcp` MCP server.

## STRUCTURE
```
groken/
├── gateway.py       # core: GatewaySession (gateway I/O) + GatewayManager (sandbox lifecycle, provisioning)
├── client.py        # Connect-RPC unary to api2.cursor.sh (SandClient, ConnectError, detect_client_version)
├── auth.py          # OAuth PKCE (redirectTarget=sand) + token store ~/.config/groken/tokens.json (0600)
├── checksum.py      # x-cursor-checksum port + machineId (ioreg)
├── config.py        # dedicated-bot binding (~/.config/groken/config.json, 0600)
├── provisioning.py  # WORKER_DESCRIPTION guardrails (data only)
├── errors.py        # ConnectError → actionable user hints
├── cli.py / mcp_server.py  # entry points
tests/               # 13 files — the behavioral contract (all network mocked)
docs/painpoints-2026-08-19.md  # design rationale (researched agent-failure modes)
```

## WHERE TO LOOK
| Task | Location |
|------|----------|
| Change chat send/receive | `gateway.py` GatewaySession (`send_prompt`, `ask`, `events`, `transcript_tail`) |
| Change sandbox/session lifecycle | `gateway.py` GatewayManager (`_ensure_sandbox`, `session`, `command`) |
| Change dedicated-bot behavior | `provisioning.py` WORKER_DESCRIPTION + `gateway.py` `own_agent_id` |
| Add CLI verb | `cli.py` dispatch map |
| Add MCP tool | `mcp_server.py` (docstring = machine-consumed tool description — do not strip) |
| Auth/login issues | `auth.py` (PKCE, refresh normalization) |
| Error messages | `errors.py` `explain_error` |

## CODE MAP
| Symbol | Module | Role |
|--------|--------|------|
| GatewayManager | gateway.py | Hub: owns EnsureSandBox→gateway session, retry-once re-mint, bot provisioning |
| GatewaySession | gateway.py | Per-pod gateway I/O: `/api/<cmd>` POST + `/events` SSE |
| SandClient | client.py | aiserver Connect-RPC unary (sandboxes list only) |
| explain_error | errors.py | failure-mode → user hint |
| WORKER_DESCRIPTION | provisioning.py | guardrail persona for the dedicated bot |

## CONVENTIONS (this project)
- uv-managed; install: `uv venv .venv && uv pip install -p .venv/bin/python -e ".[mcp]"`.
- Tests: `.venv/bin/python -m pytest tests/` — all network mocked (`httpx.MockTransport`, `monkeypatch`); no conftest, no markers.
- No CI, no committed lint config (ruff run ad hoc, defaults); typing via pyrightconfig.json (basedpyright; warnings tolerated).
- Secrets/config files are always written mode 0600.
- Env overrides: `SAND_BACKEND_URL`, `SAND_CLIENT_VERSION`, `GROKEN_BOT_NAME`.

## ANTI-PATTERNS (THIS PROJECT)
- NEVER poll where the `/events` SSE stream works; when streaming, subscribe BEFORE triggering the action.
- NEVER break the retry contract: 401→single refresh→retry (client), gateway failure→force re-mint→retry once (manager), `clientNonce` idempotency on sendPrompt/createAgent. Pinned by tests.
- NEVER remove the `updateAgent` description-upgrade in `own_agent_id` (pinned by test_guardrails).
- NEVER add fixed sleeps to bridge waiting paths — events or condition-based waits only.
- Do not "fix" intentional boundary catch-alls (ioreg fallback, webbrowser launch) without reading the WHY comments/tests.

## NOTES
- Not a git repo. `.venv` contains a stale `sand_bridge-0.1.0.dist-info` (project's previous name) — harmless.
- Client version auto-tracks the installed Grok Bot app (`/Applications/Grok Bot.app` Info.plist) — headers must match the app or calls may be gated.
- Gateway tokens are short-lived; `GatewayManager` re-mints automatically.
