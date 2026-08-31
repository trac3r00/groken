# Provider E2E Runbook

This runbook separates what **can be verified locally without a provider account**
from what **requires a real Grok Bot / Cursor account** and is therefore only
testable by someone with valid credentials. Do not conflate the two, and do not
report a mocked or local-relay check as proof against the real provider.

The repo's test suite (`tests/`) mocks all network I/O
(`httpx.MockTransport`, `monkeypatch`) and pins the wire contract. It is the
contract of record for *behavior*, but it is **not** provider E2E.

## 1. Verified locally, no provider account (always reproducible)

| Layer | What is exercised | How to run |
| --- | --- | --- |
| Full test suite | Wire contract, retry contract, idempotency, security | `.venv/bin/python -m pytest tests/` |
| Package wheel | Packaging, skill/docs/License shipped | `uv build` then inspect the wheel |
| Skill installers | `codex-skills`/`cursor-skills`/`gjc-skills`/`omo-skill`/`claude-skills` dry-run | `groken install <target> --dry-run` |
| **Share relay auth** | `require_share` 401 on missing/forged token; 200 on a valid grant; metadata-only endpoints do **not** build a live `GatewayManager` | spin `groken share serve` against an isolated `HOME` |

### Share relay: local E2E without a Bot account

Because the relay's grant auth is hashed and database-local and `/v1/bot` is a
pure metadata read, the full authenticated flow is provable with no provider
credentials:

```bash
# Isolated HOME so the real ~/.config/groken/shares.json is untouched.
export QA_HOME="$(mktemp -d)"
mkdir -p "$QA_HOME/.config/groken"

# Mint a grant directly into that isolated store.
QA_HOME="$QA_HOME" .venv/bin/python -c "
from groken.share_store import ShareStore
from pathlib import Path
g = ShareStore(Path('$QA_HOME/.config/groken/shares.json')).create('qa','agent-id','qa-bot')
print(g.token)
"

# Serve against the isolated store, foreground.
HOME="$QA_HOME" .venv/bin/groken share serve --host 127.0.0.1 --port 8787

# In a second terminal:
curl -i http://127.0.0.1:8787/v1/health            # 200 {"ok":true}
curl -i http://127.0.0.1:8787/v1/bot               # 401 (no token)
curl -i -H 'Authorization: Bearer forged' \
  http://127.0.0.1:8787/v1/bot                     # 401 (bad token)
curl -i -H 'Authorization: Bearer <real-token>' \
  http://127.0.0.1:8787/v1/bot                     # 200 {"agent_id":..,"name":..}
```

The last line returns 200 even when the serving host has **no login tokens** —
this is the behavior pinned by
`tests/test_share.py::test_bot_identity_does_not_require_live_gateway`.

## 2. NOT verified locally — requires a real provider account

These paths are **untested** in CI and in the local suite because they demand a
live, logged-in Grok Bot desktop session and a reachable Cursor gateway. They
must be exercised by a human with valid credentials before claiming
"works 100%".

| Untested boundary | Why it cannot be local | Owner-credential path to verify |
| --- | --- | --- |
| OAuth PKCE login (`groken login`) | Needs a real cursor.com browser login | Run `groken login`, follow the URL, confirm `tokens.json` populated (0600) and `accessToken`/`refreshToken` present |
| `EnsureSandBox` → gateway session | Needs a subscribed/permitted account and a live gateway | After login, run `groken bots`; confirm a roster returns |
| Real chat `sendPrompt` / `/events` SSE | Needs a live Bot pod | `groken ask "<prompt>"`; confirm a non-canned reply streams |
| `listAgents` / `updateAgent` | Needs the account's real agent IDs | `groken configure` interactively and confirm the Bot resolves |
| `share create` (real Bot grant) | Needs `listAgents` on the live account | `groken share create --name <name> --bot <bot>`; confirm a token is printed |
| Exec / VNC / computer display | Needs a forever-boxed Bot computer | `groken exec` / `groken vnc --display` against a provisioned Bot |
| Swarm fan-out | Needs multiple live Bots | `groken swarm send --bots a,b "..."` |

### How to run the one-line provider smoke (interactive, real account)

```bash
.venv/bin/groken login          # browser flow; wait for completion
.venv/bin/groken bots           # expect a roster, not an error
.venv/bin/groken ask "reply with the word PONG"   # expect "PONG", not a canned greeting
```

Credential-store expectations: `~/.config/groken/tokens.json` and
`~/.config/groken/config.json` are read/written mode 0600 and hold only this
machine's material; no backend metadata is sent with `share`.

## 3. Reporting convention

When reporting verification of any path in section 2, say **explicitly** that it
was a real-provider account E2E, or state that it remains untested. A green
`pytest` run is contract verification, **not** provider E2E.