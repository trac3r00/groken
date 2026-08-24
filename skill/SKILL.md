---
name: groken
description: Delegate tasks to a dedicated Grok Bot agent running on xAI/Cursor's always-on cloud computer. Real-time chat bridge - send a task, the Bot executes it on its cloud VM (browser, terminal, files, plugins like Slack/GitHub/Notion), replies stream back. Use when work should run on cloud compute instead of this host, needs persistent logins or browser automation on services without APIs, must survive host shutdown, or needs a stateful long-running agent. Triggers: groken, grok bot, delegate to grok, cloud computer, offload this, run it in the cloud, ask the bot.
---

# groken — omo ↔ Grok Bot bridge

Bridge into Grok Bot's own chat channel (protocol-faithful: genuine OAuth login,
app-identical gateway commands). Work runs on the Bot's persistent cloud VM.

## Default agent

All calls go to groken's **own dedicated Bot** (auto-created on first use,
default name `groken`, customize via `~/.config/groken/config.json`
`{"bot_name": "..."}` or env `GROKEN_BOT_NAME`). Never touch the user's other
Bots unless the user explicitly names one.

## Use it

CLI (direct):
```bash
~/groken/.venv/bin/groken agents                 # roster
~/groken/.venv/bin/groken ask "task"             # send + wait for reply (default Bot)
~/groken/.venv/bin/groken ask "task" --stream    # stream reply chunks to a TTY
~/groken/.venv/bin/groken send "task"            # fire-and-forget
~/groken/.venv/bin/groken tail                   # recent transcript
~/groken/.venv/bin/groken tail -n 50 --json      # structured transcript entries
~/groken/.venv/bin/groken tail --since TIMESTAMP # entries after a timestamp
~/groken/.venv/bin/groken tail --full            # complete entry bodies
~/groken/.venv/bin/groken exec COMMAND           # native remote command execution
~/groken/.venv/bin/groken service install        # install launchd services
~/groken/.venv/bin/groken service status         # inspect service presence
~/groken/.venv/bin/groken service uninstall      # remove launchd services
~/groken/.venv/bin/groken inspect-app            # inspect app command-table drift
~/groken/.venv/bin/groken vnc                   # mint a VNC URL
~/groken/.venv/bin/groken doctor                # run tiered diagnostics
```

`exec` and `vnc` are native-mcp only. They never fall back to the gateway or
another operation plane. Review native execution commands before sending them.
`doctor` checks seven tiers, tokens, gateway, controller, model, execDaemon, pod
identity, and MCP handshake, while continuing through soft failures.

MCP (any host — stdio, streamable HTTP, or SSE; see the MCP server section):
- `grok_bot_ask(text, bot?, timeout_s?)` — request/response; the primary tool
- `grok_bot_send(text, bot?)` — fire-and-forget
- `grok_bot_list()` / `grok_bot_tail(bot?)`

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
Pain-point research behind the design: `docs/painpoints-2026-08-19.md`.

## Failure handling

- Auth errors → `~/groken/.venv/bin/groken login` (browser) to refresh tokens.
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
