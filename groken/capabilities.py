from collections import Counter
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, cast

from .gateway_legacy_rows import LEGACY_GATEWAY_ROWS
from .gateway_versions import (
    CURRENT_030_NO_ARGS,
    CURRENT_030_REPLY_OVERRIDES,
    current_027_command_names,
    current_030_command_names,
)


class CommandRisk(StrEnum):
    READ_ONLY = "read_only"
    MUTATING = "mutating"
    SENSITIVE = "sensitive"
    DESTRUCTIVE = "destructive"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class GatewayCommandSpec:
    name: str
    args: str
    reply: str
    domain: str
    risk: CommandRisk


class GatewayReader(Protocol):
    def own_agent_id(self) -> str: ...
    def command(self, method: str, args: dict[str, object] | None = None) -> object: ...


_READ_ONLY = {
    "getAgentTranscriptTail",
    "openAgentTail",
    "promptAcceptanceStatus",
    "listAgents",
    "countAgents",
    "searchAgents",
    "searchMedia",
    "getCloudAgentInfo",
    "getListenerIntegrations",
    "getAgentAvatar",
    "getAgentWorkflows",
    "getConversationOutline",
    "skillsCatalog",
    "getPluginSyncStatus",
    "getSkillPublishTargets",
    "getSubagents",
    "getAsyncTasks",
    "getForeverBoxStatus",
    "getTeachRecordingStatus",
    "getTrays",
    "getAgentChannels",
    "getBoxSecretsStatus",
    "getAgentAutomations",
    "listAllAutomations",
    "isAgentNetworkEnabled",
    "isGlobalSearchEnabled",
    "isEgressTunnelAvailable",
    "getSharingState",
    "getAgentMemories",
    "getAgentNotificationAvatar",
    "getAgentThread",
    "getAgentTranscript",
    "getAgentTranscriptPage",
    "getAgentTranscriptWindow",
    "getBotTemplateExportPolicy",
    "getBoxStoreStatus",
    "getHostSettings",
    "getHostStatus",
    "getTranscript",
    "listBoxMcpServers",
    "openAgent",
    "openAgentWindowed",
    "readAttachmentChunk",
    "readAttachmentImage",
    "readAttachmentText",
}
_SENSITIVE = {
    "submitSecret",
    "getListenerConnectUrl",
    "getAutomationWebhookCredential",
    "completeMcpOAuth",
    "injectChromeCookies",
    "requestWebAuthnCeremony",
    "setBoxSecrets",
}
_DESTRUCTIVE = {
    "deleteAgents",
    "deleteAgentWorkflow",
    "unpublishSkill",
    "handBackForeverBox",
    "clearTrays",
    "disconnectChannel",
    "removeOwnAgentFromSharedRoom",
    "leaveSharedRoom",
    "deleteAgentAutomation",
    "clearAgentMemories",
    "clearBoxStoreNow",
    "deleteAgent",
    "deleteAgentMemory",
    "prepareBoxForRecreate",
    "resetForeverBox",
    "autoUpdateBoxNow",
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
    for name, args, reply, domain in LEGACY_GATEWAY_ROWS
)
LEGACY_GATEWAY_COMMANDS = GATEWAY_COMMANDS
CURRENT_027_COMMAND_NAMES = current_027_command_names(
    spec.name for spec in LEGACY_GATEWAY_COMMANDS
)
CURRENT_030_COMMAND_NAMES = current_030_command_names(CURRENT_027_COMMAND_NAMES)


def _current_command_rows() -> list[dict[str, object]]:
    legacy = {spec.name: spec for spec in LEGACY_GATEWAY_COMMANDS}
    rows: list[dict[str, object]] = []
    for name in CURRENT_030_COMMAND_NAMES:
        spec = legacy.get(name)
        rows.append(
            {
                "name": name,
                "args": "none" if name in CURRENT_030_NO_ARGS else "object",
                "reply": CURRENT_030_REPLY_OVERRIDES.get(
                    name, spec.reply if spec is not None else "unknown"
                ),
                "domain": spec.domain if spec is not None else "unverified",
                "risk": spec.risk.value
                if spec is not None
                else CommandRisk.UNKNOWN.value,
                "schema_confidence": "partial" if spec is not None else "unknown",
            }
        )
    return rows


def capability_manifest(*, include_commands: bool = True) -> dict[str, object]:
    current_rows = _current_command_rows()
    payload: dict[str, object] = {
        "official_app_version": "0.30.0",
        "bundle_version": "0.30.0",
        "embedded_package_version": "0.30.0",
        "legacy_embedded_package_version": "0.24.0",
        "gateway_command_count": len(CURRENT_030_COMMAND_NAMES),
        "legacy_gateway_command_count": len(LEGACY_GATEWAY_COMMANDS),
        "version_expectations": {
            "0.24": {"embedded_package_version": "0.24.0", "command_count": 125},
            "0.27": {"embedded_package_version": "0.27.0", "command_count": 143},
            "0.30": {"embedded_package_version": "0.30.0", "command_count": 147},
        },
        "contract_verification": {
            "names_verified": True,
            "schemas_verified": False,
            "schema_confidence": "partial",
        },
        "legacy_delta": {
            "added": sorted(
                set(CURRENT_030_COMMAND_NAMES)
                - {spec.name for spec in LEGACY_GATEWAY_COMMANDS}
            ),
            "removed": sorted(
                {spec.name for spec in LEGACY_GATEWAY_COMMANDS}
                - set(CURRENT_030_COMMAND_NAMES)
            ),
        },
        "domains": dict(Counter(str(row["domain"]) for row in current_rows)),
        "risks": dict(Counter(str(row["risk"]) for row in current_rows)),
        "local_features": {
            "harnesses": {"detect": True, "install": True, "uninstall": True},
            "routines": {"list": True, "new": True, "edit": True, "run": True},
            "env": {"capture": True, "restore": True, "current_snapshot": True},
            "update": {"status": True, "manual_trigger": True, "scheduled": False},
            "swarm": {"send": True, "rooms": True},
            "mcp": {"available": True, "confirmation_gates": True},
        },
    }
    if include_commands:
        payload["commands"] = current_rows
    return payload


def live_read_only_status(gateway: GatewayReader) -> dict[str, object]:
    box_value = gateway.command("getForeverBoxStatus", {"id": gateway.own_agent_id()})
    box = cast("dict[str, object]", box_value) if isinstance(box_value, dict) else {}
    return {
        "agent_count": gateway.command("countAgents"),
        "agent_network_enabled": None,
        "global_search_enabled": gateway.command("isGlobalSearchEnabled"),
        "egress_tunnel_available": gateway.command("isEgressTunnelAvailable"),
        "forever_box": {
            "state": box.get("state"),
            "image_update_available": box.get("imageUpdateAvailable"),
            "host_update_available": box.get("hostUpdateAvailable"),
        },
    }
