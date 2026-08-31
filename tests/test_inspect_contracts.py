from groken.capabilities import (
    CURRENT_027_COMMAND_NAMES,
    CURRENT_030_COMMAND_NAMES,
    LEGACY_GATEWAY_COMMANDS,
)
from groken.inspect_app import expected_contracts
from groken.inspect_contracts import (
    diff_contract_signatures,
    extract_command_contracts,
)


def test_versioned_expectations_prefer_embedded_package_version() -> None:
    # Given / When
    legacy_profile, legacy_names, _ = expected_contracts("0.27.0", "0.24.0")
    profile_027, names_027, _ = expected_contracts("0.27.0", "0.27.0")
    profile_030, names_030, _ = expected_contracts("0.30.0", "0.30.0")

    # Then
    assert legacy_profile == "legacy-embedded-0.24"
    assert len(legacy_names) == len(LEGACY_GATEWAY_COMMANDS) == 125
    assert profile_027 == "grok-bot-0.27"
    assert names_027 == list(CURRENT_027_COMMAND_NAMES)
    assert len(names_027) == 143
    assert profile_030 == "grok-bot-0.30"
    assert names_030 == list(CURRENT_030_COMMAND_NAMES)
    assert len(names_030) == 147


def test_unknown_or_unsupported_versions_have_no_legacy_expectation() -> None:
    # Given / When
    unknown_profile, unknown_names, _ = expected_contracts(None, None)
    unsupported_profile, unsupported_names, _ = expected_contracts("0.27.0", "9.0.0")

    # Then
    assert unknown_profile == unsupported_profile == "unknown"
    assert unknown_names == unsupported_names == []


def test_handler_fingerprint_detects_same_name_changed_target() -> None:
    # Given
    expected = extract_command_contracts(
        "var x={listAgents:t=>t.listAgents(),countAgents:t=>t.countAgents()};"
    )
    found = extract_command_contracts(
        "var x={listAgents:t=>t.countAgents(),countAgents:t=>t.countAgents()};"
    )

    # When
    diff = diff_contract_signatures(found=found, expected=expected)

    # Then
    assert diff.changed == ("listAgents",)
    assert diff.unchanged == ("countAgents",)
    assert diff.unknown == ()


def test_validator_fingerprint_detects_same_name_changed_schema() -> None:
    # Given
    expected = extract_command_contracts(
        "var x={listAgents:v().noArgs,countAgents:v().noArgs};"
    )
    found = extract_command_contracts(
        "var x={listAgents:v().args({id:s()}),countAgents:v().noArgs};"
    )

    # When
    diff = diff_contract_signatures(found=found, expected=expected)

    # Then
    assert diff.changed == ("listAgents",)
    assert diff.unchanged == ("countAgents",)
    assert diff.unknown == ()


def test_handler_without_extractable_body_is_unknown_not_unchanged() -> None:
    # Given
    expected = extract_command_contracts(
        "var x={listAgents:t=>t.listAgents(),countAgents:t=>t.countAgents()};"
    )
    found = extract_command_contracts(
        "var x={listAgents:wrappedHandler,countAgents:t=>t.countAgents()};"
    )

    # When
    diff = diff_contract_signatures(
        found=found,
        expected=expected,
        found_names=("countAgents", "listAgents"),
    )

    # Then
    assert diff.changed == ()
    assert diff.unchanged == ("countAgents",)
    assert diff.unknown == ("listAgents",)
