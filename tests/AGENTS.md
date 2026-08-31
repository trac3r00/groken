# tests/ — Behavioral Contract

Score 31 (distinct domain): the tests pin every network-facing behavior; nothing here touches the real backend. Read the pinning test BEFORE changing code it covers — most "obvious simplifications" break pinned wire/retry behavior. Root AGENTS.md lists the contracts; this file says WHERE each is pinned.

## Contract → Pinning Test
| Contract | Pinned by |
|----------|-----------|
| 401 → exactly one refresh → retry once → then raise | test_client.py |
| Sandbox session force re-mint only once; failure after refreshed auth stays an error | test_gateway.py, test_chaos.py |
| SSE-first ask; poll fallback must NOT resend prompt; `clientNonce` preserved across remint | test_events_ask.py, test_provisioning.py |
| Event ordering: arrival order, dedup, late append allowed after busy=false | test_events_ask.py, test_cli_ask_stream.py |
| `updateAgent` description upgrade in own_agent_id; cached bot name must match config | test_guardrails.py |
| 0600 modes on tokens.json / config.json / machine-id cache | test_auth.py, test_config.py, test_checksum.py |
| Refresh field normalization: snake_case → camelCase; falsy values preserve old tokens | test_auth.py |
| Idempotency: hashed keys, identical-completion retry returns original record, same-key/different-body rejected | test_controller_store.py (+ native store cases in test_native_*) |
| Worker enrollment / lease / complete flow | test_poller.py |
| Guardrail persona text shipped intact | test_guardrails.py, test_provisioning.py |
| Routine store CRUD, event hooks, builtin templates | test_routines.py, test_routine_builtin_templates.py |
| Swarm: roster-ordered sections, per-bot FAILED on partial failure, exit 0/1 split, `--rounds` 1..3 relay | test_swarm.py, test_swarm_validation.py, test_swarm_process.py, test_swarm_worker.py, test_swarm_interfaces.py |
| Native teams: createGroup once, 2–6 Bot membership, ordered members, CLI/MCP group ask, MCP confirmation | test_native_teams.py, test_mcp_confirmation.py |
| Bot add/duplicate lifecycle + CLI UX | test_bot_lifecycle.py, test_cli_bot_ux.py |
| Manual update trigger, readiness without sleeps, backend failures | test_bot_update.py, test_bot_update_readiness.py, test_update_backend_failures.py |
| Env capture manifest + native collectors | test_env_manifest.py, test_env_native_capture.py, test_env_native_runner.py, test_env_native_pending.py, test_env_persistence.py |
| Env restore: confirm-first plan, resume journal, drift, store, CLI | test_env_restore*.py (10 files incl. cli/store/manifest/resilience/production/update_seam/controller_integration) |
| MCP local-error sanitization (no path/id leaks) | test_mcp_local_errors.py |
| Grok Bot 0.30 command/payload/update-contract audit | test_inspector_version_drift.py, test_bot_provisioning_payloads.py, test_update_payload_contracts.py, test_local_diagnostics_compatibility.py |
| Share workflow: store CRUD/private paths, relay pins requests to shared bot, RelayManager stays on `/v1` endpoints, CLI lifecycle + parser routing, private removable link file | test_share.py |
| Share store: immutable bot identity + hash-only persistence, blank-field errors, malformed rows can't authenticate, concurrent create keeps records and can't resurrect a revocation | test_share_store_security.py |
| Duplicate share creation: typed store error, lock and state preservation, one typed loser under concurrency, and clean CLI failure without secret or path traceback leaks | test_share_duplicate.py |
| Share relay: bot_id pinned without name resolution, `/v1/metadata` absent (404), server-side exec returns only result, revocation withholds exec output and stops SSE streams, VNC URL 60s TTL, listAgents forced to grant id + safe-field projection, fails closed on malformed upstream, secret keys never leak | test_share_server_security.py |
| Share client: exec/vnc only via `/v1/exec`+`/v1/vnc` proxy, stream requires done frame + surfaces error frames, owner create pins resolved bot_id, connect reads token from file/prompt/stdin (never argv) and rejects public HTTP, share mode blocks local account commands pre-dispatch | test_share_client_security.py |

## Mocking Discipline
- HTTP: `httpx.MockTransport` with per-test handlers (client, gateway, events, chaos, poller); auth monkeypatches `httpx.post`/`httpx.Client`; FastAPI via TestClient.
- Shared helpers are MODULE-LOCAL (`make_client`, `make_session`, `FakeClock`, `ScriptedEvents`, fake managers/clients). There is NO conftest.py by design — keep helpers local instead of introducing shared fixtures casually.
- Async via `pytest.mark.anyio`; fs/UX assertions via `tmp_path`/`capsys`.

## COMMANDS
```bash
.venv/bin/python -m pytest tests/
```
No pytest config anywhere (pyproject/setup.cfg/pytest.ini absent) — plain defaults, single run must pass.

## Known Coverage Gaps (representative, non-exhaustive)

A missing dedicated filename isn't proof of missing behavioral coverage. The groups below identify modules without a same-named test file and state their indirect coverage. Contract rows above are the authoritative behavior map. Modules with direct coverage under a differently named file are omitted here: `cli.py` (27 importing test files), `share_store.py`/`share_server.py`/`share_client.py` (the four share security suites), `update_backend.py` (test_update_backend_failures.py), `native_mcp_server.py` (test_native_mcp_direct.py), `native_wait_models.py` (test_native_client_wait.py, test_native_wait_http.py), `env_collectors.py` (test_env_native_capture.py and the restore suites), `controller_models.py` (test_controller_store.py, test_controller_app.py), and `gateway_legacy_rows.py` (data consumed through test_capabilities.py).

- **Inspection and compatibility:** `app_archive.py` and `inspect_versions.py` are exercised indirectly by `test_inspect_app.py` and `test_inspect_contracts.py`; `gateway_versions.py` is covered by `test_inspector_version_drift.py` and the other versioned app-audit tests. `local_health.py` is exercised directly through the doctor and status compatibility checks in `test_local_diagnostics_compatibility.py`.
- **MCP and gateway operations:** `mcp_support.py` and `mcp_operations.py` are exercised directly by `test_mcp_confirmation.py`, `test_mcp_local_errors.py`, `test_mcp_operations.py`, and `test_mcp_registry.py`. `gateway_operations.py` is reached through those MCP operation tests.
- **Environment restore helpers:** `env_restore_contracts.py`, `env_restore_drift.py`, `env_restore_errors.py`, `env_restore_execution.py`, `env_restore_gateway.py`, `env_restore_inventory.py`, `env_restore_journal.py`, `env_restore_journal_codec.py`, `env_restore_lock.py`, `env_restore_plan.py`, `env_restore_report.py`, `env_restore_run.py`, `env_restore_service.py`, and `env_restore_validation.py` are covered indirectly by the ten `test_env_restore*.py` files, controller integration tests, and MCP error tests. `env_restore_manifest.py` and `env_restore_store.py` also have direct or focused coverage in `test_env_restore_manifest.py` and `test_env_restore_store.py`.
- **Swarm and private state:** `swarm_relay.py` is covered indirectly by relay and truncation assertions in `test_swarm.py` and `test_swarm_validation.py`. `swarm_rooms.py` is covered by `test_swarm_interfaces.py` and its malformed-state path in `test_mcp_local_errors.py`. `private_files.py` is covered indirectly by mode and persistence assertions in `test_auth.py`, `test_config.py`, `test_checksum.py`, and share and worker tests.
- **Data models and stores:** `native_models.py`, `native_store.py`, `worker_models.py`, and `worker_store.py` are exercised through native controller, native wait, env-native, worker app, worker runner, poller, and controller integration tests. The share support modules `share_config.py`, `share_protocol.py`, and `share_server_contracts.py` are pinned through `test_share.py`, `test_share_client_security.py`, and `test_share_server_security.py`.
- **Thin entry points and support modules:** `callback_main.py`, `controller_main.py`, `poller_main.py`, `worker_main.py`, and `native_cli.py` are covered through CLI and install smoke paths. `plugin_tools.py` is covered by `test_cli_plugin_tools.py`; `native_client.py`, `native_poller.py`, `callback_app.py`, `controller_app.py`, `worker_app.py`, and `worker_runner.py` have focused tests.
