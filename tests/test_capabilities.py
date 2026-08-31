from typing import cast

from groken.capabilities import (
    GATEWAY_COMMANDS,
    CommandRisk,
    capability_manifest,
    live_read_only_status,
)


def object_dict(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    mapping = cast("dict[object, object]", value)
    assert all(isinstance(key, str) for key in mapping)
    return {str(key): item for key, item in mapping.items()}


class FakeGateway:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object] | None]] = []

    def own_agent_id(self) -> str:
        return "bot-1"

    def command(self, method: str, args: dict[str, object] | None = None) -> object:
        self.calls.append((method, args))
        responses: dict[str, object] = {
            "countAgents": 4,
            "isGlobalSearchEnabled": True,
            "isEgressTunnelAvailable": False,
            "getForeverBoxStatus": {
                "state": "running",
                "imageUpdateAvailable": True,
                "hostUpdateAvailable": False,
                "vncUrl": "must-not-leak",
            },
        }
        return responses[method]


def test_official_gateway_manifest_versions_current_and_legacy_inventories() -> None:
    manifest = capability_manifest()
    by_name = {spec.name: spec for spec in GATEWAY_COMMANDS}

    assert manifest["official_app_version"] == "0.30.0"
    assert manifest["bundle_version"] == "0.30.0"
    assert manifest["embedded_package_version"] == "0.30.0"
    assert manifest["legacy_embedded_package_version"] == "0.24.0"
    assert manifest["version_expectations"] == {
        "0.24": {"embedded_package_version": "0.24.0", "command_count": 125},
        "0.27": {"embedded_package_version": "0.27.0", "command_count": 143},
        "0.30": {"embedded_package_version": "0.30.0", "command_count": 147},
    }
    assert manifest["gateway_command_count"] == 147
    assert manifest["legacy_gateway_command_count"] == 125
    verification = manifest["contract_verification"]
    assert verification == {
        "names_verified": True,
        "schemas_verified": False,
        "schema_confidence": "partial",
    }
    assert len(by_name) == 125
    assert by_name["getAgentMemories"].risk is CommandRisk.READ_ONLY
    assert by_name["deleteAgent"].risk is CommandRisk.DESTRUCTIVE
    assert by_name["setBoxSecrets"].risk is CommandRisk.SENSITIVE
    assert by_name["updateHostNow"].risk is CommandRisk.MUTATING
    current_commands = manifest["commands"]
    assert isinstance(current_commands, list)
    current_rows = [object_dict(row) for row in cast("list[object]", current_commands)]
    current_by_name = {str(row["name"]): row for row in current_rows}
    assert current_by_name["authenticateMcpServer"]["risk"] == "unknown"
    assert current_by_name["authenticateMcpServer"]["schema_confidence"] == "unknown"
    assert current_by_name["getBotTemplateExportPolicy"]["args"] == "none"
    assert current_by_name["sendDraft"]["args"] == "object"
    assert current_by_name["updateForeverBox"]["reply"] == "box-status"
    assert current_by_name["updateHostNow"]["reply"] == "record"
    removed_names = {
        "autoUpdateBoxNow",
        "getBoxStoreStatus",
        "isAgentNetworkEnabled",
    }
    assert removed_names.isdisjoint(current_by_name)
    assert removed_names <= set(by_name)
    legacy_delta = object_dict(manifest["legacy_delta"])
    added, removed = legacy_delta["added"], legacy_delta["removed"]
    assert isinstance(added, list)
    assert isinstance(removed, list)
    assert len(cast("list[object]", added)) == 34
    assert len(cast("list[object]", removed)) == 12


def test_live_status_calls_only_allowlisted_read_commands() -> None:
    gateway = FakeGateway()

    status = live_read_only_status(gateway)

    assert status == {
        "agent_count": 4,
        "agent_network_enabled": None,
        "global_search_enabled": True,
        "egress_tunnel_available": False,
        "forever_box": {
            "state": "running",
            "image_update_available": True,
            "host_update_available": False,
        },
    }
    assert gateway.calls == [
        ("getForeverBoxStatus", {"id": "bot-1"}),
        ("countAgents", None),
        ("isGlobalSearchEnabled", None),
        ("isEgressTunnelAvailable", None),
    ]
