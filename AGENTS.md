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
├── cli.py / mcp_server.py / mcp_operations.py / mcp_support.py  # classic chat and MCP entry points, operation dispatch, confirmation and error translation
├── routines.py      # user-owned routine store (~/.config/groken/routines/) + pre/post-update, env-restore, manual event hooks
├── swarm.py + swarm_process/relay/rooms/worker.py  # external fan-out/gather to N Bots: roster-ordered sections, partial failures, --rounds peer relay
├── native_teams.py  # persistent native createGroup teams: 2–6 members, ordered membership, one group-agent ask
├── bot_update.py / update_backend.py / gateway_operations.py  # manual Bot app/computer update trigger + condition-based readiness (no fixed sleeps) + gateway update/restore orchestration for MCP
├── exec_service.py / gateway_versions.py / gateway_legacy_rows.py  # gateway exec-protocol client + audited 0.30 and legacy 0.24 command contract data
├── share_store/server/client/config/protocol/server_contracts.py  # manual share relay: hashed revocable grants pinned to immutable bot_id, FastAPI relay (/v1 incl. exec+vnc, no metadata route), recipient-side RelayManager, validated link persistence, SSE framing + typed errors, request/adapter contracts
├── env_manifest.py / env_collectors.py / env_persistence.py / env_native_runner.py / env_restore*.py  # pod env capture + confirm-first diff-based restore (content-addressed manifests, resume journal, drift report)
├── native_cli/client/controller/executor/store/models/wait_models/poller/mcp_server  # native operation plane v2: exec/file/process ops via local controller (groken-native / groken-native-mcp)
├── worker_app/runner/store/models + controller_app/models/store + callback_app + poller(_main)  # remote OMO worker plane: FastAPI job API + durable stores + polling relay
├── service.py / installers.py / doctor.py / local_health.py / capabilities.py  # launchd+tunnel install, agent/plugin setup, layered health diagnostics, local compatibility checks, gateway capability manifest
├── vnc.py / vnc_proxy.py / vnc_ready.py / inspect_app.py / app_archive.py / inspect_contracts.py / inspect_versions.py / status.py / parsing.py / plugin_tools.py  # VNC JWT+proxy+RFB readiness, app archive inspection, contract and version checks, status reporting
├── private_files.py  # atomic 0600 text writes for private configuration and secret files
└── *_main.py        # console-script entrypoints: callback, controller, poller, worker, native
tests/               # behavioral contract (see tests/AGENTS.md); provider network mocked
docs/                # design rationale + runbooks: native-operation-plane, direct-worker-runbook, capabilities-0.30.0 (+ assets/ hero image); 0.27 historical map stays in-tree unshipped
skill/SKILL.md       # published agent-skill definition for delegating to Grok Bot
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
| Native exec/file/process ops | `native_executor.py` behind `native_controller.py`; durability/idempotency in `native_store.py` |
| Remote OMO worker jobs | `worker_app.py` (API) → `worker_runner.py` (omo subprocess); leases/jobs in `controller_store.py` |
| Polling/completion relay | `poller.py` / `native_poller.py` |
| Install as macOS launchd service | `service.py` |
| Health diagnostics | `doctor.py` (layered checks, continues past failures) |
| Gateway command capability gates | `capabilities.py` |
| Routines (list/new/edit/run, event hooks) | `routines.py` + `cli.py` dispatch |
| Swarm fan-out / ordered sections / rounds | `swarm.py` (`run_swarm`, `render`), `swarm_process.py` executor |
| Native persistent Bot teams | `native_teams.py` (`create_native_team`, `get_native_team`, `ask_native_team`) |
| Manual Bot update + readiness wait | `bot_update.py`, `update_backend.py` |
| Share relay grants / endpoints / recipient mode | `share_store.py` (hashed grants, revoke), `share_server.py` (relay app), `share_client.py` (RelayManager), `share_config.py` (link persistence), contracts in `share_server_contracts.py` + `share_protocol.py` |
| Env capture / confirm-first restore | `env_manifest.py` (capture), `env_restore_run.py` + `env_restore_service.py` (restore), journal in `env_restore_journal.py` |

## CODE MAP
| Symbol | Module | Role |
|--------|--------|------|
| GatewayManager | gateway.py | Hub: owns EnsureSandBox→gateway session, retry-once re-mint, bot provisioning |
| GatewaySession | gateway.py | Per-pod gateway I/O: `/api/<cmd>` POST + `/events` SSE |
| SandClient | client.py | aiserver Connect-RPC unary (sandboxes list only) |
| explain_error | errors.py | failure-mode → user hint |
| WORKER_DESCRIPTION | provisioning.py | guardrail persona for the dedicated bot |
| NativeStore / ControllerStore | native_store.py / controller_store.py | SHA-256 hashed idempotency keys → durable JSON records; same-key/different-request rejected |
| worker_runner | worker_runner.py | validates workspace, spawns omo subprocess per job |
| _ReplyCompletion | gateway.py | reply-done heuristic: content + 2× non-busy + 2s quiet |

## CONVENTIONS (this project)
- uv-managed; install: `uv venv .venv && uv pip install -p .venv/bin/python -e ".[mcp]"`.
- Tests: `.venv/bin/python -m pytest tests/` — all network mocked (`httpx.MockTransport`, `monkeypatch`); no conftest, no markers.
- CI is `.github/workflows/ci.yml`: macOS 15 on pushes to `main` and pull requests; it pins uv 0.11.30, syncs the locked full environment, tests Python 3.11–3.13, runs Ruff and an error-only basedpyright gate, builds artifacts, and smoke-tests the wheel-installed skill.
- Ruff 0.16.2 is locked in the dev dependency group and runs with default rules; both pyright and basedpyright use standard mode via `pyrightconfig.json`, with CI failing on type errors.
- Secrets/config files are always written mode 0600.
- Extras: `.[mcp]` (MCP server), `.[share]` (fastapi/pydantic/uvicorn/anyio for `share serve`), `.[worker]` (same stack for the worker/controller/callback services). Eight console scripts: groken, groken-mcp, groken-native, groken-native-mcp, groken-worker, groken-controller, groken-callback, groken-poller.
- Env overrides: `SAND_BACKEND_URL`, `SAND_CLIENT_VERSION`, `GROKEN_BOT_NAME`.

## ANTI-PATTERNS (THIS PROJECT)
- NEVER poll where the `/events` SSE stream works; when streaming, subscribe BEFORE triggering the action.
- NEVER break the retry contract: 401→single refresh→retry (client), gateway failure→force re-mint→retry once (manager), `clientNonce` idempotency on sendPrompt/createAgent. Pinned by tests.
- NEVER remove the `updateAgent` description-upgrade in `own_agent_id` (pinned by test_guardrails).
- NEVER add fixed sleeps to bridge waiting paths — events or condition-based waits only.
- NEVER reuse an idempotency key with a different request body — ControllerStore/NativeStore reject same-key/different-request by design (pinned).
- Do not "fix" intentional boundary catch-alls (ioreg fallback, webbrowser launch, job-boundary Exception serialization in worker_app/poller/native_poller, doctor tier continuation) without reading the WHY comments/tests.

## NOTES
- Repo: github.com/trac3r00/groken (private). Historical note: the project was previously named `sand_bridge`; a stale `sand_bridge-0.1.0.dist-info` may linger in old `.venv` trees and is harmless.
- Client version auto-tracks the installed Grok Bot app (`/Applications/Grok Bot.app` Info.plist) — headers must match the app or calls may be gated.
- Gateway tokens are short-lived; `GatewayManager` re-mints automatically.
- docs/native-operation-plane.md and docs/direct-worker-runbook.md are the authoritative design docs for the native and worker planes — read before reshaping those subsystems.
- CI runs the Python 3.11–3.13 pytest matrix, Ruff, an error-only basedpyright gate, artifact build, and wheel smoke test on macOS 15 for pushes to `main` and pull requests; there is no Makefile or committed Ruff/pytest configuration, and both type checkers use standard mode via `pyrightconfig.json`.
