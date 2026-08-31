from groken.bot_update import RestorePlan as BotRestorePlan
from groken.env_restore import RestorePlan as PublicRestorePlan
from groken.env_restore_contracts import RestorePlan as ContractRestorePlan
from groken.env_restore_plan import RestorePlan as CoreRestorePlan


def test_restore_plan_is_one_class_across_public_surfaces() -> None:
    # Given the public restore surfaces import RestorePlan
    # When the classes are compared by identity
    # Then they are the same class defined in env_restore_plan
    assert ContractRestorePlan is CoreRestorePlan
    assert PublicRestorePlan is CoreRestorePlan
    assert BotRestorePlan is CoreRestorePlan
    assert ContractRestorePlan.__module__ == "groken.env_restore_plan"
