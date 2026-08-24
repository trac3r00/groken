# Direct OMO worker runbook

The direct worker removes Grok Bot natural-language mediation from task
execution. For operations that should also bypass OMO interpretation, use the
separate native v2 plane documented in `native-operation-plane.md`.

```text
origin OMO/Hermes -> controller -> HTTPS -> outbound poller -> remote OMO
```

The Grok account still owns the Linux computer. The poller and OMO processes run
as user `box` in that computer's shared filesystem.

## Durable state

Origin controller state:

- `jobs/*.json`: complete request, status, lease, attempts, result, delivery flag
- `idempotency/*.json`: origin request digest -> stable job id

Remote worker state:

- `poller.json`: enrolled controller identity and worker token
- `secrets.json`, `model-api-key`, `omo/models.json`: complete OMO credential bundle
- `pending-completion.json`: terminal result awaiting controller acknowledgement
- `sessions/`, `logs/`: OMO session and execution evidence

All state files are created mode 0600 through same-directory temporary files,
flushed, and atomically replaced.

## Lease behavior

- A lease has a unique `lease_id` and expiry.
- A worker that loses the lease response recovers its existing unexpired lease.
- An expired lease is requeued and may be assigned to another worker.
- Completion requires the matching worker and `lease_id`.
- Identical completion retries are accepted; conflicting terminal payloads are
  rejected.
- Default lease lifetime is one hour, twice the default OMO execution timeout.

The poller persists terminal output before sending it. If the completion
response or tunnel disappears, a restarted poller sends the same completion
again without rerunning OMO.

## Controller submission

Use an idempotency key for every task that can mutate state:

```bash
curl -X POST http://127.0.0.1:18766/v1/jobs \
  -H "Authorization: Bearer $GROKEN_CONTROLLER_TOKEN" \
  -H "Idempotency-Key: $ORIGIN_SESSION_ID:$REQUEST_ID" \
  -H "Content-Type: application/json" \
  --data '{"task":"...","workspace":"project","origin_session_id":"..."}'
```

A retry with the same key and body returns the original job. Reusing the key
with a different body returns HTTP 409.

Workspace paths must be relative and cannot contain `..`. OMO runs with the
workspace permission preset; that preset is a policy boundary, not an OS
sandbox. Only trusted controller clients should be allowed to submit prompts.

## Poller recovery

`groken-poller`:

1. Refuses to send a stored token to a controller URL different from the one
   used at enrollment.
2. Rejects incomplete enrollment state.
3. Flushes a pending completion before leasing new work.
4. Retries tunnel/network errors and 429/5xx responses with bounded exponential
   backoff.
5. Fails closed on authentication errors.

## Gateway chat recovery

Groken's chat path now:

- Reuses one `clientNonce` when SSE falls back to transcript polling.
- Waits for both `isComposingMessage=false` and `isRunning=false` before
  declaring an SSE answer complete.
- Refreshes account authentication at most once per sandbox ensure.
- Remints stale gateway sessions for CLI and MCP send/ask/tail/events paths.
- Does not remint on semantic gateway 500 responses, avoiding duplicate
  mutations caused by invalid arguments.

## Native operations

The same enrolled poller also leases `/v2` native operations. These are exact
terminal argv and byte-oriented file requests; they do not create OMO sessions
or model calls. Native and OMO queues, leases, results, and idempotency mappings
are stored separately.

Native smoke test:

```bash
groken-native --workspace maintenance exec -- /usr/bin/true
```

## Operations

Health checks:

```bash
groken doctor
groken capabilities
curl -fsS http://127.0.0.1:18766/healthz
```

Inspect a job:

```bash
curl -H "Authorization: Bearer $GROKEN_CONTROLLER_TOKEN" \
  http://127.0.0.1:18766/v1/jobs/$JOB_ID
```

Upgrade the remote worker only from a checksum-verified wheel. Restart the
poller after an upgrade; existing enrollment files remain valid only when the
controller URL is unchanged.

## Remaining trust boundary

A prompt can use OMO's bash/edit tools inside the remote account. It is not safe
to accept untrusted public job submissions. A stronger deployment should add a
dedicated UID/container, mount only the selected workspace, cap CPU/memory/PIDs
and output, and restrict egress to the model and controller endpoints.
