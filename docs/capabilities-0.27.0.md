# Historical Grok Bot capability map (through 0.27.0)

Historical evidence date: 2026-08-26 (0.27.0 audit); baseline evidence date:
2026-08-21 (0.23.0). For the current 0.30.0 audit, see
[`capabilities-0.30.0.md`](capabilities-0.30.0.md). Sources:

- Official app archive: `/Applications/Grok Bot.app/Contents/Resources/app.asar`
- Coordinator bundle: `dist/node-agent-coordinator/main.cjs`
- Renderer bundle: `dist/renderer/assets/index-DwRIoOiS.js`
- Live account, queried only with commands classified read-only
- Public workspace/runtime source: <https://github.com/xai-org/grok-build>

This is an inventory, not an arbitrary-command API. Groken deliberately does not
expose a generic `command(method, args)` tool because the same registry contains
secret submission, approval resolution, deletion, publishing, sharing, and
computer handback operations.

## Historical: 0.27.0 state (2026-08-26)

The audited 0.27.0 app exposed 143 gateway commands. At that time, the
`groken capabilities` manifest reported the 0.27 command rows and legacy delta against the
audited baseline, per-domain and per-risk counts, and contract-verification
confidence. `groken inspect-app --fail-on-drift` verifies the inventory against
the installed bundle, including versioned critical fingerprints for
hash-mismatched 0.27 builds. Update commands verified for 0.27 include the
image-precedence `updateForeverBox({id})` path used by `groken bot update`.

Groken exposes the read-only `grok_bot_status` MCP tool, while sensitive,
mutating, and destructive gateway additions remain unavailable through generic
automation.

### Historical: 0.24.0 update (2026-08-25)

The 0.24.0 app exposed 125 gateway commands: all 87 baseline commands below
plus 38 additions, with no removals or renames. The additions covered agent
memories and transcript pagination, host/store/MCP status, attachments, MCP
OAuth, WebAuthn, box recreation/update controls, cookies, secrets, and
destructive reset/delete operations.

The detailed inventory below remains the sourced 0.23.0 baseline and protocol
rationale; it is historical evidence, not the current command count.

## Architecture found

The desktop renderer does not call the pod gateway directly. It talks to a local
`node-agent-coordinator`, whose typed command registry validates argument and
reply shapes. The coordinator obtains/reuses the account sandbox, discovers the
pod gateway, routes commands, consumes SSE events, rewrites VNC URLs, and
remints connections after transport failure.

The official bundle separates:

1. 20 desktop/coordinator IPC commands for host settings, attachments, MCP,
   cookies, box secrets, window focus, health, and developer fault injection.
2. 87 pod-gateway commands grouped into 16 domains.
3. Five explicitly handled SSE channels: `transcript`, `agents`,
   `agent-upserted`, `forever-box`, and `mcp-oauth-pending`.

The cloud computer is account-scoped. Bot shell, GUI terminal, and direct OMO
are different processes and environments inside the same Linux namespaces and
filesystem.

## Desktop/coordinator IPC commands (20)

Read/host operations:

- `uploadAttachment`, `readAttachmentImage`, `readAttachmentText`,
  `readAttachmentChunk`
- `getHostSettings`, `getHostStatus`
- `listBoxMcpServers`, `refreshMcp`
- `listAgents`, `getConversationOutline`, `getSubagents`

Mutating/sensitive operations:

- `setHostSettings`, `setBoxSecrets`, `injectChromeCookies`
- `updateForeverBox`, `setWindowFocused`
- `createAgent`, `deleteAgents`
- `setDevGatewayOffline`, `setGatewayPaused`

The last two are app fault-injection controls and must never be surfaced through
normal Groken automation.

## Pod-gateway registry (87)

Risk legend: `R` read-only, `W` mutating, `S` sensitive, `D` destructive or
state-removing.

### Transcript and send

- R: `getAgentTranscriptTail`, `openAgentTail`, `promptAcceptanceStatus`
- W: `sendPrompt`, `reactToMessage`

### Widgets and approvals

- W: `respondToWidget`, `resolveAutoReviewApproval`,
  `resolveLocalToolPermission`, `dismissWidget`, `voteFeedback`
- S: `submitSecret`

### Roster, search, and cloud-agent state

- R: `listAgents`, `countAgents`, `searchAgents`, `searchMedia`,
  `getCloudAgentInfo`, `getAgentAvatar`
- W: `createAgent`, `createGroup`, `setGroupMembers`, `updateAgent`,
  `duplicateAgent`, `kickstartAgent`, `interruptAgentRun`,
  `requestDiskSaverAudit`, `broadcastToAgents`, `setAgentUnread`,
  `setAgentHiddenFromSidebar`, `setAgentNotificationsEnabled`,
  `setAgentNotifyOnUpdates`, `setAgentAvatarBytes`
- D: `deleteAgents`

### Listener integrations

- R: `getListenerIntegrations`
- S: `getListenerConnectUrl`

### Workflows

- R: `getAgentWorkflows`
- W: `createAgentWorkflow`, `updateAgentWorkflow`, `runAgentWorkflowNow`,
  `importAgentWorkflowText`, `importAgentWorkflowUrl`, `portAgentLocalSkills`
- D: `deleteAgentWorkflow`

### Skills

- R: `skillsCatalog`, `getPluginSyncStatus`, `getSkillPublishTargets`
- W: `syncPluginSkills`, `publishSkill`, `resyncPublishedSkill`
- D: `unpublishSkill`

### Subagents and asynchronous work

- R: `getSubagents`, `getAsyncTasks`

### Account computer

- R: `getForeverBoxStatus`, `isEgressTunnelAvailable`
- W: `ensureForeverBox`
- D: `handBackForeverBox`

### Teach recording

- R: `getTeachRecordingStatus`
- W: `startTeachRecording`, `stopTeachRecording`

### Trays

- R: `getTrays`
- W: `dismissTray`
- D: `clearTrays`

### Channels

- R: `getAgentChannels`
- W: `connectChannel`, `refreshChannel`
- D: `disconnectChannel`

### Secrets

- R: `getBoxSecretsStatus` (status and key names only; not values)

### Automations

- R: `getAgentAutomations`, `listAllAutomations`
- S: `getAutomationWebhookCredential`
- W: `setAgentAutomationEnabled`, `createAgentAutomation`,
  `updateAgentAutomation`, `runAgentAutomationNow`
- D: `deleteAgentAutomation`

### Feature gates

- R: `isAgentNetworkEnabled`, `isGlobalSearchEnabled`

### Sharing and rooms

- R: `getSharingState`
- W: `createRoomFromAgent`, `createRoomInvite`, `joinSharedRoom`,
  `respondToRoomJoinRequest`, `createSharedRoom`, `addOwnAgentToSharedRoom`,
  `setSharedRoomTyping`
- D: `removeOwnAgentFromSharedRoom`, `leaveSharedRoom`

## Live read-only observations

Live probes used the same typed registry and returned only redacted shapes:

- Four agents were visible.
- The separate Forever Box status reported `running` during the first probe and
  later `absent`; this is not the same as proving the account computer is absent.
- Forever Box image update was available; host update was not.
- Agent network gate was disabled.
- Global search gate was enabled.
- Egress tunnel feature was unavailable.
- 1,285 catalog skills were visible.
- The dedicated Groken agent saw 36 workflows, two channel manifests, no
  connected channels, no automations, no subagents, and no asynchronous tasks.
- Account-wide automation listing returned three existing automations.
- Sharing was disabled.

These observations can change and should be queried with
`groken capabilities`; they are not hard-coded behavior.

## Expected findings confirmed

- Every Bot shares one account computer and filesystem.
- Each Bot has a separate screen, not a separate security boundary.
- Gateway commands use a short-lived pod connection and network token.
- VNC URLs are rewritten by the coordinator and may carry embedded access
  material; they must never be logged as ordinary status output.
- Bot shell, GUI terminal, and direct OMO have different cwd/PATH/TTY/process
  state despite shared namespaces and files.

## New findings

- The official app has a complete typed gateway registry, not an unstructured
  free-form RPC surface.
- `forever-box` is a first-class SSE channel with VNC-window state.
- The coordinator has DNS/wildcard routing and explicit paused/offline fault
  injection, explaining why naive direct pod URLs are less stable than the app.
- Computer handback, disk-saver audit, teach recording, channel connections,
  shared rooms, publishing, and automation-webhook credentials all exist in the
  same gateway; a generic passthrough would be unsafe.
- Safe status operations are broad enough to provide observability without
  exposing mutation.

## Groken policy

Groken exposes the full inventory as metadata and a small live read-only status
probe. New executable commands must be added one at a time with:

1. An observed official request shape.
2. A response model.
3. A risk classification.
4. An idempotency/retry decision.
5. A focused test.
6. Explicit user approval for destructive or credential-bearing operations.
