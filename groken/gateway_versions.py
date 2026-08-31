"""Versioned gateway name deltas observed from read-only app inspection."""

from collections.abc import Iterable

CURRENT_027_ADDED_COMMANDS = frozenset(
    {
        "authenticateMcpServer",
        "createAgentFromTemplate",
        "deleteBotTemplate",
        "dismissUserForm",
        "generateAgentAvatarImage",
        "getBotTemplateForSourceAgent",
        "getBotTemplateVersion",
        "getEffectiveMcpPlugins",
        "getMcpCatalog",
        "getMcpPluginLogo",
        "getMcpState",
        "getVoiceCall",
        "installMcpEntry",
        "listBotTemplates",
        "listMcpServerTools",
        "nudgeVoiceCall",
        "publishBotTemplate",
        "readVoiceCallAgentContext",
        "readVoiceCallSentMessages",
        "recordVoiceCall",
        "removeMcpAccount",
        "removeMcpServer",
        "renameMcpAccount",
        "setMcpCustomInstructions",
        "submitUserForm",
        "toggleMcpToolDisabled",
        "transcribeAudio",
        "uninstallMcpPlugin",
        "updateMcpPluginInstall",
    }
)
CURRENT_027_REMOVED_COMMANDS = frozenset(
    {
        "appendConnectorCard",
        "autoUpdateBoxNow",
        "clearBoxStoreNow",
        "deleteAgent",
        "getBoxStoreStatus",
        "prepareBoxForRecreate",
        "resetForeverBox",
        "resumeBoxAfterRecreate",
        "setBoxMigrating",
        "snapshotBoxStoreNow",
        "updateBotTemplate",
    }
)
CURRENT_030_ADDED_COMMANDS = frozenset(
    {
        "discardDraft",
        "getBotTemplateExportPolicy",
        "resolveVirtualCardApproval",
        "sendDraft",
        "setBotTemplateVisibility",
    }
)
CURRENT_030_REMOVED_COMMANDS = frozenset({"isAgentNetworkEnabled"})
CURRENT_027_ASAR_SHA256 = (
    "8517a4ca7e7c986f1321de6165720645e4889df23687a4231a529b6b2a252162"
)
CURRENT_027_HOST_SHA256 = (
    "15f375d8ab80818e6ec084ad0f0ce457831bfa1408715597e3b4398a6501b417"
)
CURRENT_030_ASAR_SHA256 = (
    "4bbcd2f7af9f54cd1b354bd7b3c8376da569657a80f6560edac9b3280299a394"
)
CURRENT_030_HOST_SHA256 = (
    "c1b6b79bb3830a0cdafab5f7629dfde1c7fb89a48ecd7bb32c52343164415b3f"
)
CURRENT_027_CRITICAL_FINGERPRINTS = {
    "createAgent": "d0a481de7806b46f5d4ac657d8799d8bbf476743bfefb500238c3adc41c2347c",
    "duplicateAgent": "2b0e8432f8278e3e02539c86e90258b54095950367f33bbcdadc92876e8d647c",
    "getForeverBoxStatus": "69e923ff2d569d33d9574914b2860d8deb57ebc905ee9dc8f5111f66ec20fb80",
    "updateAgent": "a9087d22398f92cb9a8fe9f223cba6545d64da4f5b17d763a25e6d61bb7cbadf",
    "updateForeverBox": "45e8bd50cfdd88f9888bfb96e5cc5b9859381d2df78498b782ab11750c71bf1c",
    "updateHostNow": "98c04d5eca1b9d3a74ae790f1edb4193b1cff87b8c60847438c91c67eb0ea9de",
}
CURRENT_030_CRITICAL_FINGERPRINTS = {
    "createAgent": "d36341e2966365a62ce2588b78c5361a63b5e988f40f451f212d8756001960a0",
    "duplicateAgent": "3015c63ab03323420713adcb6bf584653e7cbd6099ea44572303d5a4f159ab08",
    "getForeverBoxStatus": "83351d10b7b6e4341b1e5a69b909ca48d11e717b7dea49e80749fe9c9754056f",
    "updateAgent": "ff25423a04af392bc289f063e1876bec7a6c197c67f9b4f59c71d62150667346",
    "updateForeverBox": "56ff8cde647cab1ab151009fa9a0045c320c08b8a50f85ff52561bc69f8c37ec",
    "updateHostNow": "6b338b4d1db85121446ec3fcaa36cd81da8050878a873b11e8d0756224645549",
}
CURRENT_027_REPLY_OVERRIDES = {
    "getHostStatus": "host-status",
    "updateHostNow": "record",
}
CURRENT_030_REPLY_OVERRIDES = CURRENT_027_REPLY_OVERRIDES
CURRENT_027_NO_ARGS = frozenset(
    {
        "clearTrays",
        "countAgents",
        "getBoxSecretsStatus",
        "getEffectiveMcpPlugins",
        "getHostSettings",
        "getListenerIntegrations",
        "getMcpCatalog",
        "getMcpState",
        "getPluginSyncStatus",
        "getSharingState",
        "getSkillPublishTargets",
        "getTeachRecordingStatus",
        "getTranscript",
        "getTrays",
        "isAgentNetworkEnabled",
        "isEgressTunnelAvailable",
        "isGlobalSearchEnabled",
        "listAgents",
        "listAllAutomations",
        "listBotTemplates",
        "skillsCatalog",
        "syncPluginSkills",
    }
)
CURRENT_030_NO_ARGS = frozenset(
    (CURRENT_027_NO_ARGS - CURRENT_030_REMOVED_COMMANDS)
    | {"getBotTemplateExportPolicy"}
)


def current_027_command_names(legacy_names: Iterable[str]) -> tuple[str, ...]:
    return tuple(
        sorted(
            (set(legacy_names) - CURRENT_027_REMOVED_COMMANDS)
            | CURRENT_027_ADDED_COMMANDS
        )
    )


def current_030_command_names(current_027_names: Iterable[str]) -> tuple[str, ...]:
    return tuple(
        sorted(
            (set(current_027_names) - CURRENT_030_REMOVED_COMMANDS)
            | CURRENT_030_ADDED_COMMANDS
        )
    )
