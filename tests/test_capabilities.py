from groken.capabilities import (
    GATEWAY_COMMANDS,
    CommandRisk,
    capability_manifest,
    live_read_only_status,
)


class FakeGateway:
    def __init__(self) -> None:
        self.methods: list[str] = []

    def command(self, method: str, args: dict[str, object] | None = None) -> object:
        _ = args
        self.methods.append(method)
        responses: dict[str, object] = {
            "countAgents": 4,
            "isAgentNetworkEnabled": False,
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


def test_official_gateway_manifest_has_all_087_commands() -> None:
    manifest = capability_manifest()
    by_name = {spec.name: spec for spec in GATEWAY_COMMANDS}

    assert manifest["official_app_version"] == "0.23.0"
    assert manifest["gateway_command_count"] == 87
    assert len(by_name) == 87
    assert by_name["getForeverBoxStatus"].risk is CommandRisk.READ_ONLY
    assert by_name["deleteAgents"].risk is CommandRisk.DESTRUCTIVE
    assert by_name["submitSecret"].risk is CommandRisk.SENSITIVE


def test_live_status_calls_only_allowlisted_read_commands() -> None:
    gateway = FakeGateway()

    status = live_read_only_status(gateway)

    assert status == {
        "agent_count": 4,
        "agent_network_enabled": False,
        "global_search_enabled": True,
        "egress_tunnel_available": False,
        "forever_box": {
            "state": "running",
            "image_update_available": True,
            "host_update_available": False,
        },
    }
    assert set(gateway.methods) == {
        "countAgents",
        "isAgentNetworkEnabled",
        "isGlobalSearchEnabled",
        "isEgressTunnelAvailable",
        "getForeverBoxStatus",
    }
