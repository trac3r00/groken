from collections import Counter
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, cast


class CommandRisk(StrEnum):
    READ_ONLY = "read_only"
    MUTATING = "mutating"
    SENSITIVE = "sensitive"
    DESTRUCTIVE = "destructive"


@dataclass(frozen=True)
class GatewayCommandSpec:
    name: str
    args: str
    reply: str
    domain: str
    risk: CommandRisk


class GatewayReader(Protocol):
    def command(self, method: str, args: dict[str, object] | None = None) -> object: ...


_ROWS = (
    ("getAgentTranscriptTail", "object", "transcript-page", "transcript"),
    ("openAgentTail", "object", "transcript-page", "transcript"),
    ("sendPrompt", "object", "send-result", "send"),
    ("promptAcceptanceStatus", "object", "acceptance-lookup", "send"),
    ("respondToWidget", "object", "record-or-null", "widgets"),
    ("resolveAutoReviewApproval", "object", "void", "approvals"),
    ("resolveLocalToolPermission", "object", "void", "approvals"),
    ("dismissWidget", "object", "record", "widgets"),
    ("submitSecret", "object", "void", "widgets"),
    ("reactToMessage", "object", "void", "send"),
    ("voteFeedback", "object", "void", "widgets"),
    ("listAgents", "none", "array", "roster"),
    ("countAgents", "none", "count", "roster"),
    ("searchAgents", "object", "array", "roster"),
    ("searchMedia", "object", "array", "search"),
    ("createAgent", "object", "record", "roster"),
    ("createGroup", "object", "record", "roster"),
    ("setGroupMembers", "object", "record-or-null", "roster"),
    ("updateAgent", "object", "record-or-null", "roster"),
    ("deleteAgents", "object", "record", "roster"),
    ("duplicateAgent", "object", "record", "roster"),
    ("kickstartAgent", "object", "record-or-null", "roster"),
    ("interruptAgentRun", "object", "record", "roster"),
    ("requestDiskSaverAudit", "object", "record-or-null", "roster"),
    ("broadcastToAgents", "object", "record", "roster"),
    ("getCloudAgentInfo", "object", "record-or-null", "cloud_agents"),
    ("getListenerIntegrations", "none", "record", "listeners"),
    ("getListenerConnectUrl", "object", "connect-url", "listeners"),
    ("setAgentUnread", "object", "void", "roster"),
    ("setAgentHiddenFromSidebar", "object", "void", "roster"),
    ("setAgentNotificationsEnabled", "object", "void", "roster"),
    ("setAgentNotifyOnUpdates", "object", "void", "roster"),
    ("setAgentAvatarBytes", "object", "record-or-null", "roster"),
    ("getAgentAvatar", "object", "record", "roster"),
    ("getAgentWorkflows", "object", "array", "workflows"),
    ("createAgentWorkflow", "object", "array", "workflows"),
    ("updateAgentWorkflow", "object", "array", "workflows"),
    ("deleteAgentWorkflow", "object", "array", "workflows"),
    ("runAgentWorkflowNow", "object", "void", "workflows"),
    ("importAgentWorkflowText", "object", "import-result", "workflows"),
    ("importAgentWorkflowUrl", "object", "import-result", "workflows"),
    ("portAgentLocalSkills", "object", "import-result", "workflows"),
    ("getConversationOutline", "object", "array", "transcript"),
    ("skillsCatalog", "none", "array", "skills"),
    ("syncPluginSkills", "none", "array", "skills"),
    ("getPluginSyncStatus", "none", "record", "skills"),
    ("getSkillPublishTargets", "none", "record", "skills"),
    ("publishSkill", "object", "record", "skills"),
    ("resyncPublishedSkill", "object", "record", "skills"),
    ("unpublishSkill", "object", "record", "skills"),
    ("getSubagents", "object", "array", "subagents"),
    ("getAsyncTasks", "object", "array", "subagents"),
    ("getForeverBoxStatus", "object", "box-status", "computer"),
    ("ensureForeverBox", "object", "box-status", "computer"),
    ("handBackForeverBox", "object", "void", "computer"),
    ("startTeachRecording", "object", "record", "teach"),
    ("stopTeachRecording", "object", "record", "teach"),
    ("getTeachRecordingStatus", "none", "record", "teach"),
    ("getTrays", "none", "array", "trays"),
    ("dismissTray", "object", "void", "trays"),
    ("clearTrays", "none", "void", "trays"),
    ("getAgentChannels", "object", "channels-view", "channels"),
    ("connectChannel", "object", "channels-view", "channels"),
    ("disconnectChannel", "object", "channels-view", "channels"),
    ("refreshChannel", "object", "channels-view", "channels"),
    ("getBoxSecretsStatus", "none", "box-secrets", "secrets"),
    ("getAgentAutomations", "object", "array", "automations"),
    ("getAutomationWebhookCredential", "object", "record", "automations"),
    ("listAllAutomations", "none", "array", "automations"),
    ("isAgentNetworkEnabled", "none", "boolean", "capabilities"),
    ("isGlobalSearchEnabled", "none", "boolean", "capabilities"),
    ("isEgressTunnelAvailable", "none", "boolean", "computer"),
    ("getSharingState", "none", "record", "sharing"),
    ("createRoomFromAgent", "object", "record", "sharing"),
    ("createRoomInvite", "object", "record", "sharing"),
    ("joinSharedRoom", "object", "record", "sharing"),
    ("respondToRoomJoinRequest", "object", "record", "sharing"),
    ("createSharedRoom", "object", "record", "sharing"),
    ("addOwnAgentToSharedRoom", "object", "record", "sharing"),
    ("removeOwnAgentFromSharedRoom", "object", "record", "sharing"),
    ("setSharedRoomTyping", "object", "void", "sharing"),
    ("leaveSharedRoom", "object", "record", "sharing"),
    ("setAgentAutomationEnabled", "object", "array", "automations"),
    ("createAgentAutomation", "object", "array", "automations"),
    ("updateAgentAutomation", "object", "array", "automations"),
    ("deleteAgentAutomation", "object", "array", "automations"),
    ("runAgentAutomationNow", "object", "void", "automations"),
)

_READ_ONLY = {
    "getAgentTranscriptTail", "openAgentTail", "promptAcceptanceStatus",
    "listAgents", "countAgents", "searchAgents", "searchMedia",
    "getCloudAgentInfo", "getListenerIntegrations", "getAgentAvatar",
    "getAgentWorkflows", "getConversationOutline", "skillsCatalog",
    "getPluginSyncStatus", "getSkillPublishTargets", "getSubagents",
    "getAsyncTasks", "getForeverBoxStatus", "getTeachRecordingStatus",
    "getTrays", "getAgentChannels", "getBoxSecretsStatus",
    "getAgentAutomations", "listAllAutomations", "isAgentNetworkEnabled",
    "isGlobalSearchEnabled", "isEgressTunnelAvailable", "getSharingState",
}
_SENSITIVE = {"submitSecret", "getListenerConnectUrl", "getAutomationWebhookCredential"}
_DESTRUCTIVE = {
    "deleteAgents", "deleteAgentWorkflow", "unpublishSkill", "handBackForeverBox",
    "clearTrays", "disconnectChannel", "removeOwnAgentFromSharedRoom",
    "leaveSharedRoom", "deleteAgentAutomation",
}


def _risk(name: str) -> CommandRisk:
    if name in _READ_ONLY:
        return CommandRisk.READ_ONLY
    if name in _SENSITIVE:
        return CommandRisk.SENSITIVE
    if name in _DESTRUCTIVE:
        return CommandRisk.DESTRUCTIVE
    return CommandRisk.MUTATING


GATEWAY_COMMANDS = tuple(
    GatewayCommandSpec(name, args, reply, domain, _risk(name))
    for name, args, reply, domain in _ROWS
)


def capability_manifest(*, include_commands: bool = True) -> dict[str, object]:
    payload: dict[str, object] = {
        "official_app_version": "0.23.0",
        "gateway_command_count": len(GATEWAY_COMMANDS),
        "domains": dict(Counter(spec.domain for spec in GATEWAY_COMMANDS)),
        "risks": dict(Counter(spec.risk.value for spec in GATEWAY_COMMANDS)),
    }
    if include_commands:
        payload["commands"] = [
            {
                "name": spec.name,
                "args": spec.args,
                "reply": spec.reply,
                "domain": spec.domain,
                "risk": spec.risk.value,
            }
            for spec in GATEWAY_COMMANDS
        ]
    return payload


def live_read_only_status(gateway: GatewayReader) -> dict[str, object]:
    box_value = gateway.command("getForeverBoxStatus", {})
    box = cast("dict[str, object]", box_value) if isinstance(box_value, dict) else {}
    return {
        "agent_count": gateway.command("countAgents"),
        "agent_network_enabled": gateway.command("isAgentNetworkEnabled"),
        "global_search_enabled": gateway.command("isGlobalSearchEnabled"),
        "egress_tunnel_available": gateway.command("isEgressTunnelAvailable"),
        "forever_box": {
            "state": box.get("state"),
            "image_update_available": box.get("imageUpdateAvailable"),
            "host_update_available": box.get("hostUpdateAvailable"),
        },
    }
