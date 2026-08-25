# groken

## Release 0.2.0

Version 0.2.0 adds the native execution and diagnostics surfaces described below.

Real-time bridge into Grok Bot chat, built as a protocol-faithful client of the
official desktop app: genuine Cursor OAuth PKCE login (`redirectTarget=sand`),
app-identical headers/checksum, and the same per-sandbox gateway the app uses.

## How it works (verified live)

1. OAuth PKCE sign-in against cursor.com, token issued to the real app client.
2. `aiserver.v1.GrokBotService/EnsureSandBox` (Connect-RPC, api2.cursor.sh)
   returns the pod's `gatewayUrl`, `gatewayToken`, `networkToken`.
3. All chat happens on the pod gateway:
   - `POST {gatewayUrl}/api/<command>` with `Authorization: Bearer <gatewayToken>`
     and `x-anyrun-network-token: <networkToken>`
   - `listAgents` -> roster (id/name/running)
   - `sendPrompt { agentId, prompt, clientNonce }` -> `{accepted: true}`
   - `getAgentTranscriptTail { id }` -> recent entries
   - `GET /events?channels=...` -> SSE realtime stream
4. The gateway token is short-lived; `GatewayManager` re-runs EnsureSandBox on
   failure and retries once. `clientNonce` makes sends idempotent.

## Install (fresh machine)

```bash
git clone <this repo> ~/groken && cd ~/groken   # or copy the directory
uv venv .venv && uv pip install -p .venv/bin/python -e ".[mcp]"
```

Uninstall: delete `~/groken`, `~/.config/groken/`, the `groken` entry in
`~/.hermes/config.yaml` `mcp_servers:`, and `~/.agents/skills/groken/`.

## Setup

```bash
cd ~/groken
.venv/bin/groken login             # sign in with the Grok Bot account
.venv/bin/groken list              # * marks this machine's configured Bot
.venv/bin/groken configure         # choose interactively
.venv/bin/groken configure groken  # or choose by name/id
.venv/bin/groken connect           # open the configured Bot's computer
.venv/bin/groken connect repo-ops  # one-off connection; default is unchanged
```

## CLI

```bash
.venv/bin/groken list                           # list Bots; * marks configured default
.venv/bin/groken configure [BOT]                # set this machine's default Bot
.venv/bin/groken connect [BOT]                  # auto-connect configured/named Bot VNC
.venv/bin/groken status                         # Bot, host, storage, secrets, and MCP health
.venv/bin/groken tools list [SERVER ...]        # discover connected plugin tools
.venv/bin/groken tools call SERVER TOOL --args-json '{}' --yes
.venv/bin/groken agents                         # legacy raw roster
.venv/bin/groken send "text" [agent]            # send, without waiting
.venv/bin/groken ask "text" [agent]             # send and wait for the reply
.venv/bin/groken ask "text" --stream            # stream reply chunks to a TTY
.venv/bin/groken tail [agent]                   # recent transcript entries
.venv/bin/groken tail [agent] -n 50 --json      # structured, bounded output
.venv/bin/groken tail [agent] --since TIMESTAMP  # entries after an ISO timestamp
.venv/bin/groken tail [agent] --full             # include complete entry bodies
.venv/bin/groken exec COMMAND [--cwd DIR]       # native remote command execution
.venv/bin/groken service install                # install controller and tunnel services
.venv/bin/groken service status                 # show service presence
.venv/bin/groken service uninstall              # remove groken services
.venv/bin/groken inspect-app                    # compare app command table with groken
.venv/bin/groken inspect-app --fail-on-drift    # exit nonzero when drift is found
.venv/bin/groken vnc                             # open configured Bot's computer and auto-connect
.venv/bin/groken vnc --display 1                 # explicitly override with display N
.venv/bin/groken doctor                         # run tiered diagnostics
.venv/bin/groken events                         # raw SSE event stream
.venv/bin/groken sandboxes                      # cloud computer status
.venv/bin/groken refresh                        # refresh tokens manually
```

`status` is secret-safe: it reports the configured Bot display, host/update
state, box storage, secret application state, and broken/connected box MCP
servers without returning secret names or values.

`tools list` queries the backend plugin catalog using the existing Grok Bot
session; plugin OAuth tokens remain backend-side. `tools call` executes one
exact server/tool/JSON operation and always requires interactive confirmation
or explicit `--yes`. A timeout may be indeterminate for mutating tools, so do
not retry unless repeating the operation is safe.

`tail --json` emits structured entries. Use `-n` or `--limit` to bound the
result, `--since` to filter by timestamp, and `--full` when abbreviated bodies
are not enough. `ask --stream` is intended for interactive terminals and falls
back to the normal reply when output is not a TTY.

`doctor` runs seven secret-safe tiers: tokens, gateway, controller, model,
execDaemon, pod identity, and MCP self-handshake. It continues through soft
failures and returns failure when authentication or the gateway is unavailable.
`inspect-app` checks the installed app bundle for command-table drift.

`vnc` resolves the locally configured Bot, reads that Bot's official
`getForeverBoxStatus`, ensures its computer when absent, waits for a live RFB
connection, and opens noVNC with autoconnect enabled. Use `--display N` only to
explicitly override the Bot-derived display.

`exec` uses the native-mcp operation plane. `vnc` uses the Bot gateway's computer
status plus a token-injecting loopback proxy; it never asks another Bot or guesses
from account-wide Chrome processes. Native execution is remote, so review the
command, working directory, and timeout before running it.

## Dedicated Bot (auto-provisioned)

First use creates a Bot named `groken` on the account (gateway `createAgent`,
idempotent via clientNonce) and caches its id in `~/.config/groken/config.json`.
All CLI/MCP calls default to it; the user's other Bots are never touched unless
explicitly named. Custom name: `{"bot_name": "..."}` in config.json or
`GROKEN_BOT_NAME` env. The config is local to each installation, so multiple
machines can bind to the same account Bot or choose different names independently.
Each machine runs its own loopback VNC proxy; concurrent viewers are supported by
the shared cloud desktop.

## What groken connects

```
your agent/script ── groken (CLI | MCP) ── Grok Bot gateway ── dedicated Bot
                                                              ├─ cloud VM (terminal / browser / files)
                                                              ├─ Bot plugins (Slack, GitHub, Notion, Google Drive,
                                                              │   Composio, Browserbase, AWS Agents, Context7, ...)
                                                              ├─ routines & schedules (server-side, survives shutdown)
                                                              └─ your local Mac (via the app's local-exec daemon)
```

**Agent hosts**
- Hermes: already registered (`mcp_servers.groken` in `~/.hermes/config.yaml`).
- omo/senpi: skill installed at `~/.agents/skills/groken` — say "groken으로 위임" to route.
- Any MCP client (Claude Desktop, Cursor, Zed): add the stdio server
  `{"mcpServers": {"groken": {"command": "~/groken/.venv/bin/groken-mcp"}}}`.

**Scripts & automation**
- Shell pipelines: `groken ask "..."` exits 0 with the reply on stdout — composes with
  `cron`/`launchd`, CI steps, or `xargs` fan-out.
- Reactive flows: `groken events` streams gateway SSE — pipe into `jq` to trigger on
  transcript appends or agent state flips.

**Through the Bot (indirect reach)**
- Anything the Bot's plugins touch (Slack post, GitHub issue, Notion page...) is one
  `ask` away — the Bot executes with its own logins on the cloud computer.
- Bot-side routines can fire on Slack/GitHub events and report back into the same
  conversation groken reads.

## Guardrails & errors

The dedicated Bot's standing instructions (verify post-action state, never
silently delete, ask before destructive ops, stop-and-report instead of
looping) live in `groken/provisioning.py` (`WORKER_DESCRIPTION`); existing Bots
are upgraded in place via `updateAgent` on next resolution. CLI errors are
translated to actionable hints by `groken/errors.py`. Design rationale:
`docs/painpoints-2026-08-19.md`.

## Install into your AI agents (you choose)

```bash
groken install                # interactive: pick from detected agents
groken install --all          # every detected agent, no prompt
groken install codex cursor   # exactly these
groken install --dry-run      # show what would change
groken uninstall              # same selection UX, removes groken entries
```

Nothing is installed without an explicit choice: bare `groken install` lists the
detected agents and waits for a selection, and in a non-interactive shell it
refuses with the detected list instead of guessing. Merges preserve sibling
servers, re-running is idempotent, and every touched file gets a `.groken-bak`
copy first.

Run `groken` with no arguments for the first-run guide (login → install → doctor).

| Agent | Surface written |
|---|---|
| Claude Code | `~/.claude.json` mcpServers + `~/.claude/skills/groken/` |
| Claude Desktop | `claude_desktop_config.json` mcpServers |
| Codex | `~/.codex/config.toml` `[mcp_servers.groken]` |
| Cursor | `~/.cursor/mcp.json` |
| VS Code / Copilot | `Code/User/mcp.json` servers |
| Gemini CLI | `~/.gemini/settings.json` |
| OpenCode | `~/.config/opencode/opencode.json` mcp |
| Windsurf | `~/.codeium/windsurf/mcp_config.json` |
| Kiro | `~/.kiro/settings/mcp.json` |
| Hermes | `~/.hermes/config.yaml` mcp_servers |
| omo/senpi | `~/.agents/skills/groken/` |

Agents that are not installed are skipped, not errored.

## MCP server (works with every MCP host)

```bash
groken-mcp                                  # stdio (Claude Desktop, Cursor, Zed, Hermes, omo)
groken-mcp --transport http --port 8321     # streamable HTTP  -> http://127.0.0.1:8321/mcp
groken-mcp --transport sse  --port 8322     # legacy SSE       -> http://127.0.0.1:8322/sse
```

All three transports serve the same seven tools. stdio config (most hosts):

```json
{ "mcpServers": { "groken": { "command": "/Users/bob/groken/.venv/bin/groken-mcp" } } }
```

Remote/containerized hosts that speak streamable HTTP point at `http://<host>:8321/mcp`;
older SSE-only clients use `http://<host>:8322/sse`. Bind beyond localhost with
`--host 0.0.0.0` only behind your own auth layer — the server itself is unauthenticated
and inherits your Grok Bot session.

Bot tools: `grok_bot_list`, `grok_bot_send`, `grok_bot_ask`,
`grok_bot_capabilities`, and `grok_bot_tail`. Plugin tools:
`grok_plugin_list` and `grok_plugin_call`; calls are blocked until
`confirmed=true` is supplied for the exact reviewed operation. Bot arguments
accept an id or display name; omit them to use the dedicated groken Bot.

## Remote OMO worker

The optional worker turns a computer with OMO installed into a narrow HTTP job
runner. It never exposes a shell endpoint: clients submit an OMO task scoped to
a relative workspace, poll by job id, and may request a signed completion
callback.

```bash
uv pip install -p .venv/bin/python -e ".[worker]"
export GROKEN_WORKER_BOOTSTRAP_TOKEN="$(openssl rand -hex 32)"
groken-worker --host 127.0.0.1 --port 8765 \
  --workspace-root /workspace \
  --omo-command ~/.local/bin/omo
```

Bootstrap exactly once over an authenticated HTTPS tunnel. The model key and
worker token are stored in owner-only files; do not send them through Bot chat
or command-line arguments.

```bash
curl -X POST https://worker.example.com/v1/bootstrap \
  -H "X-Bootstrap-Token: $GROKEN_WORKER_BOOTSTRAP_TOKEN" \
  -H "Content-Type: application/json" \
  --data @bootstrap.json
```

Submit work asynchronously and correlate the completion to the originating
session:

```json
{
  "task": "Run the checkout QA suite and report verified results",
  "workspace": "qa/checkout",
  "origin_session_id": "session-abc",
  "callback_url": "https://controller.example.com/v1/callbacks",
  "callback_token": "a-long-random-bearer-token"
}
```

`POST /v1/jobs` returns HTTP 202 and a `job_id`; `GET /v1/jobs/{job_id}` returns
the durable status record. The local `groken-callback` relay authenticates the
completion, appends it to an owner-only JSONL ledger, and emits
`remote.worker.completed` into Clawhip.

Bind both services to loopback. Put them behind a named tunnel or private
network with its own access policy, use distinct random tokens for worker and
callback authentication, and rotate the one-time bootstrap token by restarting
the worker without it after provisioning.

### Direct mode: no Grok Bot in the task path

When inbound tunnels to the sandbox are unavailable, run the controller on the
origin computer and let `groken-poller` maintain an outbound HTTPS connection.
The Grok Bot is not messaged or consulted: the remote OMO process leases tasks,
executes them in `/workspace`, and posts the result directly to the controller.

```text
origin OMO/Hermes -> local controller <- outbound remote poller -> remote OMO
```

Start the controller on the origin and expose only that loopback port through a
private or named HTTPS tunnel:

```bash
export GROKEN_CONTROLLER_TOKEN="$(openssl rand -hex 32)"
export GROKEN_ENROLLMENT_TOKEN="$(openssl rand -hex 32)"
export GROKEN_REMOTE_WORKER_TOKEN="$(openssl rand -hex 32)"
export GROKEN_MODEL_BASE_URL="https://models.example.com/v1"
export GROKEN_MODEL_API_KEY="..."
groken-controller --host 127.0.0.1 --port 18766
```

Enroll and start the remote worker once:

```bash
GROKEN_CONTROLLER_URL="https://controller.example.com" \
GROKEN_ENROLLMENT_TOKEN="..." \
groken-poller --worker-id groken-box
```

Enrollment stores the controller token and model configuration in owner-only
files. Subsequent poller restarts require only the controller URL; job traffic
uses the worker token. Submit locally with `POST /v1/jobs`, poll
`GET /v1/jobs/{job_id}`, and receive the Clawhip event
`remote.worker.completed` when the remote OMO run finishes.

## Capability and operations references

- [`docs/capabilities-0.23.0.md`](docs/capabilities-0.23.0.md) — all 87 typed
  gateway commands, 20 coordinator commands, risk classes, and verified live
  read-only observations.
- [`docs/direct-worker-runbook.md`](docs/direct-worker-runbook.md) — durable
  leases, idempotent submission/completion, poller recovery, and trust boundary.
- [`docs/native-operation-plane.md`](docs/native-operation-plane.md) — direct
  terminal/file APIs, native CLI/MCP usage, proven ExecService metadata, and
  browser/gateway adapter contracts.

## Tests

```bash
.venv/bin/python -m pytest tests/
```

Covers: checksum vs JS reference, Connect envelope codec (round-trip, split
chunks, error frames), request headers/body shape, 401 -> refresh -> retry,
stream wire format.

## Notes

- Tokens live at `~/.config/groken/tokens.json` (0600); bot binding in `~/.config/groken/config.json`.
- Env overrides: `SAND_BACKEND_URL`, `SAND_CLIENT_VERSION`, `GROKEN_BOT_NAME`.
- Undocumented internal API: schema drift is handled by re-extracting from the
  newest app bundle (`app.asar`), the same way this client was built.
