# Native operation plane

The native v2 plane executes typed operations on the account computer without
Grok Bot or OMO receiving, interpreting, or rewriting them.

```text
CLI / MCP -> controller /v2 -> outbound native poller -> terminal or file adapter
```

## Implemented operations

### `terminal.exec`

Executes an exact argv array with no implicit shell:

```json
{
  "target": "groken-box",
  "workspace": "project",
  "operation": {
    "type": "terminal.exec",
    "argv": ["/usr/bin/git", "status", "--porcelain"],
    "cwd": ".",
    "stdin_b64": "",
    "env": {},
    "timeout_ms": 30000
  }
}
```

- `argv[0]` must be absolute.
- Arguments are passed exactly; no joining or interpolation occurs.
- stdin, stdout, and stderr are byte-safe base64.
- Retained stdout and stderr are capped at 1 MiB each.
- Timeout is explicit and nonzero exit is a structured result, not a protocol
  failure.
- The process runs as the remote worker user. This is native code execution,
  not an OS sandbox.

A caller can explicitly choose `/bin/bash -lc ...`; that is visible shell
semantics selected by the caller, never inferred by Groken.

### `terminal.shell`

Runs a caller-supplied script only when the caller explicitly selects `bash` or
`posix-sh`. Groken never infers shell semantics from a command string.

### Files

- `file.read`: bounded byte ranges, total size, EOF, SHA-256
- `file.write`: byte-exact create/replace/upsert, optional SHA-256 precondition,
  same-directory temporary file, fsync, atomic replace
- `file.delete`: regular-file deletion with optional SHA-256 precondition
- `file.move`: non-overwriting move by default, optional replace and source hash
- `file.grep`: bounded UTF-8 regular-expression matches with line numbers
- `file.stat`: size, mode, kind, optional SHA-256
- `file.list`: sorted bounded directory listing

Operation paths are relative to the selected workspace and reject traversal or
symlink escapes. Terminal programs themselves can access any path available to
the worker UID.

### Processes

- `process.list`: bounded PID, PPID, user, state, and command records with
  optional substring filtering
- `process.kill`: explicit `TERM`, `INT`, or `KILL` signal to a PID greater than
  one

Process kill is intentionally separate from listing and requires a distinct
mutating operation plus idempotency key.

## Controller lifecycle

Native state is independent from prompt jobs:

- `/v2/operations`
- `/v2/operations/{operation_id}`
- `/v2/worker/lease`
- `/v2/worker/complete`

The native queue has separate files, leases, idempotency mappings, and delivery
state. Leases are capability- and target-filtered. Identical completion retries
are accepted and conflicting completions are rejected.

The combined poller runs OMO and native loops concurrently. Native waits while
enrollment is being established instead of cancelling the OMO enrollment loop.

## CLI

```bash
export GROKEN_CONTROLLER_TOKEN=...

groken-native --workspace project exec -- /usr/bin/pwd
groken-native --workspace project file-write note.txt --text 'hello'
groken-native --workspace project file-read note.txt
groken-native --workspace project shell --script 'printf native'
groken-native --workspace project file-grep note.txt '^hello'
groken-native process-list --query groken-poller
groken-native process-kill PID --signal TERM
groken-native get OPERATION_ID
```

Use `--idempotency-key` for every operation that can mutate state.

## MCP

`groken-native-mcp` is a separate server so installing ordinary Groken MCP does
not silently grant native code execution. Its typed tools are:

- `native_terminal_exec`
- `native_file_read`
- `native_file_write_text`
- `native_file_delete`
- `native_file_move`
- `native_file_grep`
- `native_terminal_shell`
- `native_process_list`
- `native_process_kill`
- `native_operation_get`
- `direct_cloud_exec` (always registered; executes a command directly through the
  ExecService client, bypassing the durable operation queue and its idempotency
  records; use the queued tools when you need durability or replay safety)
- `native_vnc_url` (registered only when the VNC capability is enabled; mints
  the current sandbox's noVNC URL, which carries access material and must be
  treated as a secret)

A default handshake therefore lists 11 tools, or 12 with VNC enabled.

Network transports bind loopback by default. The controller bearer token comes
from the environment, not MCP arguments.

## Proven official direct endpoints

`EnsureSandBox` (first proven against the 0.23.0 app; the field set is
historical evidence from that audit) returns these native computer fields in
addition to the gateway:

- `execDaemonUrl`
- `execDaemonAuthToken`
- `vncUrl`
- `terminalsFolder`
- `gatewayUrl`, `gatewayToken`, `networkToken`
- pod, tenant, and cluster identity

The official host bundle connects to `execDaemonUrl` with binary Connect-RPC
service `agent.v1.ExecService/Exec`. The stream carries typed messages:

- `init`
- `execClientMessage`
- `execServerMessage`
- `envSetRequest` / `envSetResponse`
- `mcpLoadRequest` / `mcpLoadResponse`

It constructs typed shell, read-file, write-file, glob, grep, process-list,
kill-process, and computer-use accessors. The VNC endpoint is a separate URL and
its access material must never be logged.

The current production native plane uses the installed outbound poller because
it has durable leases, idempotency, replay-safe completion, and does not expose
short-lived sandbox credentials to clients. A future `ExecDaemonAdapter` can
replace the transport beneath the same operation models without changing CLI or
MCP contracts.

## Browser adapter

The next native adapter should consume the same `/v2` queue and implement one
exclusive browser transaction containing typed actions:

- navigate
- click
- fill
- press
- wait-for
- read-text
- screenshot

Two backends are viable:

1. Official computer-use accessor over `ExecService`, after the generated
   request schemas are extracted into a standalone client.
2. A worker-local Playwright/agent-browser profile, when login separation is
   acceptable.

Do not expose arbitrary JavaScript evaluation, raw CDP, cookie export, or VNC
URLs through general MCP tools. Browser actions should return structured
results and hold a profile lock so concurrent operations cannot interleave.

## Gateway adapter

Gateway operations run on the origin beside OAuth state, not on the remote
computer. Initial native gateway tools should remain fixed read-only adapters:

- list agents
- computer/Forever Box status
- feature gates
- workflows, channels, automations, and asynchronous task status

The current audited command inventory is in `capabilities-0.30.0.md`; historical
0.23/0.24/0.27 detail remains in `capabilities-0.27.0.md`. Mutations should be
added one typed command at a time with known request schema, response schema,
risk, nonce/idempotency behavior, and unknown-outcome handling. A generic
`gateway.command` is intentionally absent.

## Unavoidable boundaries

“Native” removes agent interpretation; it cannot remove:

- account authentication and short-lived sandbox credentials
- Linux UID/filesystem permissions
- network policy
- durable controller/poller software
- protocol and payload limits
- ambiguous external side effects after a process or network crash

Those are system boundaries, not Grok Bot restrictions.
