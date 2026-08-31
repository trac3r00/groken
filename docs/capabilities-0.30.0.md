# Grok Bot capability map (0.30.0 current)

Current evidence date: 2026-08-31. Sources:

- Official app archive: `/Applications/Grok Bot.app/Contents/Resources/app.asar`
- Coordinator bundle: `dist/node-agent-coordinator/main.cjs`
- Groken's versioned command expectations and critical fingerprints
- Live account queries limited to operations classified read-only

This is an audited inventory, not an arbitrary-command API. Groken deliberately
does not expose a generic `command(method, args)` tool because the same registry
contains secret submission, approval resolution, deletion, publishing, sharing,
and computer-handback operations.

## Verified 0.30.0 state

The installed 0.30.0 app exposes 147 gateway commands. `groken inspect-app
--fail-on-drift` verifies all command names plus six critical handler/validator
fingerprints against the installed bundle. The audited reference hashes are:

- app ASAR: `4bbcd2f7af9f54cd1b354bd7b3c8376da569657a80f6560edac9b3280299a394`
- coordinator: `c1b6b79bb3830a0cdafab5f7629dfde1c7fb89a48ecd7bb32c52343164415b3f`

Compared with 0.27.0, 0.30.0 adds `discardDraft`,
`getBotTemplateExportPolicy`, `resolveVirtualCardApproval`, `sendDraft`, and
`setBotTemplateVisibility`, and removes `isAgentNetworkEnabled`. Shared command
argument modes remain unchanged. The public live-status field for agent-network
availability is retained as unavailable (`null`) rather than calling the removed
command.

The tested update path remains `updateForeverBox({id})` for a Bot image update
and `updateHostNow({force: true})` when only the host needs an update.

## Verification confidence

`groken capabilities` is authoritative for the current inventory and reports:

- command names: verified
- critical fingerprints: verified for the audited 0.30.0 hashes
- request/reply schemas: partial
- newly discovered commands without observed schemas: unavailable to generic
  automation and classified conservatively

A same-name critical handler change, an unknown build hash, a command addition,
or a command removal makes `groken inspect-app --fail-on-drift` fail closed.

## Safe live status

The read-only status probe uses only:

- `getForeverBoxStatus({id})`
- `countAgents`
- `isGlobalSearchEnabled`
- `isEgressTunnelAvailable`

It does not expose VNC URLs, tokens, secrets, generic gateway dispatch, or
mutating operations.

## Compatibility policy

A gateway operation is added only after its argument shape, reply shape, risk,
retry behavior, and approval boundary are observed and covered by a focused
test. Unknown operations remain inventory-only. Authentication, explicit
mutation confirmations, and human takeover for password, 2FA, CAPTCHA, and
macOS security prompts are not bypassed.

Historical protocol detail and the 0.23/0.24/0.27 evolution remain in
`docs/capabilities-0.27.0.md` (in-tree, not shipped in the wheel).
