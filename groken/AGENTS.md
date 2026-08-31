# groken/ — Package Internals

Score 35 (89 files, 100% code, highest import centrality). Four planes share the package: the classic chat bridge (GatewayManager/GatewaySession over per-pod HTTP+SSE), the native operation plane v2 (exec/file/process via a local controller), the remote OMO worker plane (FastAPI job API + polling relay), and the manual share relay (Bearer-token grants pinned to an immutable bot_id, foreground FastAPI relay, recipient-side RelayManager). Project-wide contracts live in the root AGENTS.md — not repeated here.

## WHERE TO LOOK
| Task | Location |
|------|----------|
| One chat round-trip end-to-end | `cli.py` `_main_impl` → `cmd_gask`/`cmd_gsend` → `_manager()` → GatewayManager `ask`/`send_prompt` → `session()` → `_ensure_sandbox()` → SandClient `list_sandboxes` → GatewaySession `ask` |
| SSE-first ask vs poll fallback | `gateway.py` `_ask_via_events` / `_ask_via_poll`; completion heuristic `_ReplyCompletion.complete` |
| Re-mint eligibility | `gateway.py` `_should_remint`: 0,401,403,404,408,429,502,503,504 + httpx.TransportError/TimeoutException |
| Gateway auth headers | GatewaySession `_headers()` (Bearer + `x-anyrun-network-token`); backend headers in `client.py` `unary()` (checksum, client type/version, ghost mode, connect-protocol-version) |
| MCP chat tools and operations | `mcp_server.py` `grok_bot_*`; `mcp_operations.py` dispatches gateway/env tools, `mcp_support.py` handles confirmation and error translation; sync gateway wrapped via `asyncio.to_thread` |
| Native op execution | `native_executor.py` (anyio.to_thread.run_sync) behind `native_controller.py` routes; durable ops in `native_store.py` |
| Worker job lifecycle | `worker_app.py` (FastAPI, anyio.Lock) → `worker_runner.py`; enrollment/lease flow in `controller_app.py` + `controller_store.py` |
| Callback relay | `callback_app.py` (FastAPI) |
| Share grant storage / relay app / recipient client | `share_store.py` (sha256-hashed tokens, flock transactions, revoke) → `share_server.py` (`/v1/*` incl. `/v1/exec` + `/v1/vnc`, NO `/v1/metadata`) → `share_client.py` (RelayManager swapped in by `cli.py` `_manager()` when a share link exists); link persistence in `share_config.py`, typed contracts in `share_server_contracts.py`, SSE framing/errors in `share_protocol.py` |
| launchd/tunnel install | `service.py`; agent/plugin installation in `installers.py` |
| Installed-app inspection | `inspect_app.py` and `app_archive.py` (ASAR metadata and bundle reads, raises AsarError on malformed packages); `inspect_contracts.py` and `inspect_versions.py` compare critical command contracts |
| Native persistent Bot teams | `native_teams.py`; CLI/MCP adapters create groups, list ordered `memberIds`, and ask the group agent |
| Local compatibility health | `local_health.py` read-only environment checks used by `doctor.py` and `status.py` |
| Private file writes | `private_files.py` atomic 0600 text writes used by config, checksum, share, and worker state |

## CONVENTIONS (beyond root)
- Every durable write: tmp file → fsync → os.replace → chmod 0600 (auth, config, checksum, all stores).
- Gateway HTTP is synchronous; async surfaces (MCP, FastAPI) wrap it with `asyncio.to_thread` — never call blocking I/O directly inside async handlers.
- Client requests emulate Cursor/Sand headers exactly and must match the installed Grok Bot app version (`detect_client_version`).
- Module-level singletons: MCP `server`; GatewayManager caches one `_session`; config caches bot_id/bot_name/client_version and validates them in `own_agent_id()`.
- State layout under ~/.config/groken: tokens.json, config.json; worker adds secrets.json, model-api-key, omo/models.json; controller/native keep separate jobs/+idempotency dirs.
- Share relay redacts secret keys from every proxied payload (`_safe_value`), pins every request to the grant's immutable `bot_id` (no name resolution), and revalidates the token mid-stream so revocation cuts off exec output and SSE frames.
- Two pollers are NOT interchangeable: `poller.py` relays worker jobs (worker plane), `native_poller.py` relays native operations (native plane); each imports its own models/store pair.
- The inspection split is intentional: `app_archive.py` reads the installed bundle, `inspect_contracts.py` fingerprints minified gateway shapes, `inspect_versions.py` compares version expectations, and `gateway_versions.py` stores the audited 0.30 contract data.
- `mcp_operations.py` is the operation layer for MCP, while `mcp_support.py` owns confirmation markers, typed annotations, and local error translation.

## ANTI-PATTERNS
- Do not strip docstrings from `mcp_server.py`/`native_mcp_server.py` tools — they are machine-consumed tool descriptions.
- Do not send gateway requests bypassing `_headers()` — `x-anyrun-network-token` must ride along or the pod rejects.
- Broad `except Exception` exists ONLY at serialization boundaries (worker_app, poller, native_poller → typed failure state; doctor → continue tiers; exec_service → normalize unavailable metadata). Do not add new broad catches elsewhere; do not remove these.
