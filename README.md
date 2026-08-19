# groken

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
.venv/bin/groken login     # opens cursor.com; sign in with the Grok Bot account
.venv/bin/groken bots      # list Bots -> bcId
```

## CLI

```bash
.venv/bin/groken agents               # list Bots (id, name, running)
.venv/bin/groken send "text" [agent]  # send (default: groken's own Bot)
.venv/bin/groken ask "text" [agent]   # send and wait for the reply
.venv/bin/groken tail [agent]         # recent transcript entries
.venv/bin/groken agents               # list Bots (id, name, running)
.venv/bin/groken events               # raw SSE event stream
.venv/bin/groken sandboxes            # cloud computer status (aiserver)
.venv/bin/groken refresh              # refresh tokens manually

## Dedicated Bot (auto-provisioned)

First use creates a Bot named `groken` on the account (gateway `createAgent`,
idempotent via clientNonce) and caches its id in `~/.config/groken/config.json`.
All CLI/MCP calls default to it; the user's other Bots are never touched unless
explicitly named. Custom name: `{"bot_name": "..."}` in config.json or
`GROKEN_BOT_NAME` env.

## Guardrails & errors

The dedicated Bot's standing instructions (verify post-action state, never
silently delete, ask before destructive ops, stop-and-report instead of
looping) live in `groken/provisioning.py` (`WORKER_DESCRIPTION`); existing Bots
are upgraded in place via `updateAgent` on next resolution. CLI errors are
translated to actionable hints by `groken/errors.py`. Design rationale:
`docs/painpoints-2026-08-19.md`.

## MCP server (for Hermes)

```bash
.venv/bin/groken-mcp      # stdio MCP server
```

Tools: `grok_bot_list`, `grok_bot_send`, `grok_bot_ask` (send + wait for reply),
`grok_bot_tail`. All accept a Bot id or its display name.

Hermes MCP config entry:

```json
{
  "mcpServers": {
    "grok-bot": {
      "command": "/Users/bob/groken/.venv/bin/groken-mcp"
    }
  }
}
```

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
