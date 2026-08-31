---
name: groken
description: Delegate tasks to a dedicated Grok Bot agent running on xAI/Cursor's always-on cloud computer. Real-time chat bridge - send a task, the Bot executes it on its cloud VM (browser, terminal, files, plugins like Slack/GitHub/Notion), replies stream back. Use when work should run on cloud compute instead of this host, needs persistent logins or browser automation on services without APIs, must survive host shutdown, or needs a stateful long-running agent. Triggers: groken, grok bot, delegate to grok, cloud computer, offload this, run it in the cloud, ask the bot, share my bot, share relay.
---

# groken — agent harness ↔ Grok Bot bridge

Bridge from any agent harness (omo, gjc, Codex, Claude Code, Cursor, OpenCode, or
anything that loads a `SKILL.md`) into Grok Bot's own chat channel (protocol-faithful: genuine OAuth login,
app-identical gateway commands). Work runs on the Bot's persistent cloud VM.

## Default agent

All calls go to groken's **own dedicated Bot** (auto-created on first use,
default name `groken`, customize via `~/.config/groken/config.json`
`{"bot_name": "..."}` or env `GROKEN_BOT_NAME`). Never touch the user's other
Bots unless the user explicitly names one. This binding is local to each machine;
multiple installations may share the same account Bot or set different
`GROKEN_BOT_NAME` values without changing one another's local config.

## Use it

CLI (direct):
```bash
groken list                   # roster; * marks configured default
groken configure [BOT]        # choose this machine's default Bot
groken connect [BOT]          # auto-connect configured/named Bot VNC
groken status                 # Bot, host, secrets, MCP, and local health
groken tools list [SERVER...] # discover connected plugin tools
groken tools call SERVER TOOL --args-json '{}' --yes
groken agents                 # legacy raw roster
groken ask "task"             # send + wait for reply (default Bot)
groken ask "task" --stream    # stream reply chunks to a TTY
groken send "task"            # fire-and-forget
groken tail                   # recent transcript
groken tail -n 50 --json      # structured transcript entries
groken tail --since TIMESTAMP # entries after a timestamp
groken tail --full            # complete entry bodies
groken exec COMMAND           # native remote command execution
groken service install        # install launchd services
groken service status         # inspect service presence
groken service uninstall      # remove launchd services
groken inspect-app            # inspect 0.30 app command-table drift
groken vnc                    # open configured Bot's computer and auto-connect
groken vnc --display 1        # explicitly override with display N
groken doctor                 # run tiered diagnostics
groken bot add NAME           # create a Bot
groken bot duplicate SRC NEW  # duplicate a Bot
groken bot update [BOT]       # manual update, env preserved
groken bot env capture [BOT]  # snapshot package/app inventory
groken bot env restore [BOT]  # confirm-first inventory restore
groken routine list           # list local routines + templates
groken routine run NAME       # run a local routine
groken team create NAME --bots a,b  # persistent native Grok Bot group
groken team members NAME      # ordered native team membership
groken team ask NAME "task"   # one message; native Bots coordinate
groken swarm send --bots a,b "task"  # external fan out, per-bot answers
groken swarm rooms            # read-only shared-room listing
groken share create --name N --bot B  # owner: mint one revocable token, printed once
groken share list             # owner: grants without token values
groken share revoke NAME      # owner: revoke a grant (kills live streams too)
groken share serve            # owner: manual foreground relay on 127.0.0.1:8787
groken share connect URL --token-file PATH  # guest: token via file/prompt/stdin, never argv
groken share status           # guest: relay URL without its token
groken share disconnect       # guest: back to local login mode
```

`vnc` resolves the locally configured Bot, reads that Bot's official computer
status, ensures its computer when absent, waits for live RFB, and opens noVNC
with autoconnect enabled. `--display N` is an explicit override.

`status` is secret-safe. `tools list` keeps OAuth credentials on the Grok
backend. Every `tools call` requires an interactive confirmation or `--yes`;
never retry an indeterminate mutating call unless repetition is safe.

`exec` is native-mcp only. `vnc` uses the configured Bot's gateway computer status
plus a local token-injecting proxy; it never guesses from another Bot's Chrome
process. Review native execution commands before sending them.

`share` grants Bearer-token access to ONE Bot, pinned to its immutable id: no
OAuth delegation, no sandbox credentials or metadata sent to the guest. While a
share link is saved, ask/send/tail/events/exec/vnc go through the relay's `/v1`
endpoints and account-level commands are blocked. The relay is a foreground
process; the owner is responsible for TLS/tunnel when exposing it beyond
loopback. The boundary is account control, not VM isolation: Bots on one
account share files and browser sessions, and exec is pod-wide.

`doctor` checks secret-safe tiers (tokens, gateway, controller, model,
execDaemon, pod identity, MCP handshake, app compatibility) while continuing
through soft failures.

MCP (any host — stdio, streamable HTTP, or SSE; see the MCP server section):
- `grok_bot_ask(text, bot?, timeout_s?)` — request/response; the primary tool
- `grok_bot_send(text, bot?)` — fire-and-forget
- `grok_bot_list()` / `grok_bot_tail(bot?)` / `grok_bot_status()`
- `grok_bot_add` / `grok_bot_duplicate` — Bot creation; blocked until `confirmed=true`
- `grok_bot_update_status` / `grok_bot_update_trigger` — manual updates only
- `grok_env_capture` / `grok_env_restore` — package/app inventory snapshot and confirm-first restore
- `grok_routine_list` / `grok_routine_run` — local routine store
- `grok_team_create` / `grok_team_members` / `grok_team_ask` — persistent native Bot groups; creation requires confirmation
- `grok_swarm_send` — external concurrent fan-out with per-bot answer sections
- `grok_plugin_list(server?)` — discover backend plugin tools
- `grok_plugin_call(server, tool, arguments_json, bot?, confirmed?)` — blocked until `confirmed=true`

## Latency expectations

- send accepted: ~0.03s; chat reply: ~3s
- cloud-computer tasks (terminal/browser): first reply ~30s, budget 60–600s
- `ask` polls the transcript (2s granularity); for sub-second updates use
  `groken events` (SSE)

## Hard rules

- All calls default to the dedicated groken Bot, whose standing instructions carry
  guardrails: verify the post-action state before reporting, never silently delete
  or overwrite, ask before destructive/irreversible operations, and stop-and-report
  instead of looping. Update them by editing `WORKER_DESCRIPTION` in
  `groken/provisioning.py` — existing Bots get upgraded in place on next use.
- All Bots share ONE cloud computer: files, browser sessions, logins visible to
  every Bot. Never send credentials through chat.
- Passwords/2FA/CAPTCHA force human takeover — design tasks to avoid them.
- Prefer `ask` with a clear deliverable; instruct the Bot to report the verified
  post-action state, not just what it attempted.

## Errors

CLI errors are translated to actionable hints (auth → `groken login`, unroutable
sandbox → retry/`groken doctor`, unknown gateway method → update the Grok Bot app).

## Failure handling

- Auth errors → `groken login` (browser) to refresh tokens.
- Gateway session re-mints automatically on failure; persistent failures mean
  the sandbox is recovering — retry after a minute.
- App update broke calls? Re-extract `app.asar` from the newest DMG (x.ai/bot)
  and diff the gateway command table; client version auto-tracks the app.

## Pain-points mapping

| Pain point | Surface or guardrail |
|---|---|
| Context loss during long work | `ask --stream` gives immediate progress; `tail --json`, `--since`, and `--full` preserve readable transcript state |
| Runaway loops and destructive commands | `exec` is native-mcp only, with no plane fallback; the dedicated Bot must verify state and ask before destructive actions |
| Cost and outage anxiety | `doctor` reports tiered health checks and actionable failures |
| App and protocol drift | `inspect-app` compares the installed app command table and can fail with `--fail-on-drift` |
| Cloud computer access | `vnc` mints an authenticated URL and never falls back to another plane |
| Persistent local operations | `service install`, `service status`, and `service uninstall` manage the controller and tunnel services |

Internals, architecture, and tests: `~/groken/README.md`
(`cd ~/groken && .venv/bin/python -m pytest tests/`).
Run any command with `--help` for exact options; the shipped `--help` output is
the factual authority when this file and the CLI ever disagree.
