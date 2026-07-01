from meridian.cli.startup.catalog import COMMAND_CATALOG
from meridian.cli.startup.classify import classify_invocation
from meridian.cli.startup.policy import RootSource, StartupClass, StateRequirement


def test_doctor_is_read_rootless_without_pre_dispatch_state() -> None:
    descriptor = COMMAND_CATALOG.get(("doctor",))
    assert descriptor is not None
    assert descriptor.startup_class is StartupClass.READ_ROOTLESS
    assert descriptor.bootstrap_plan.state_requirement is StateRequirement.NONE


def test_qi_root_is_read_rootless_without_pre_dispatch_state() -> None:
    descriptor = COMMAND_CATALOG.get(("qi",))
    assert descriptor is not None
    assert descriptor.startup_class is StartupClass.READ_ROOTLESS
    assert descriptor.bootstrap_plan.state_requirement is StateRequirement.NONE


def test_config_show_is_read_rootless_without_pre_dispatch_state() -> None:
    descriptor = COMMAND_CATALOG.get(("config", "show"))
    assert descriptor is not None
    assert descriptor.startup_class is StartupClass.READ_ROOTLESS
    assert descriptor.bootstrap_plan.state_requirement is StateRequirement.NONE


def test_argv_root_commands_have_no_pre_dispatch_bootstrap() -> None:
    for command_path in [("init",), ("config", "init")]:
        descriptor = COMMAND_CATALOG.get(command_path)
        assert descriptor is not None
        assert descriptor.root_source is RootSource.ARGV
        assert descriptor.bootstrap_plan.state_requirement is StateRequirement.NONE


def test_create_commands_opt_in_to_cwd_auto_init() -> None:
    for command_path in [(), ("spawn",), ("spawn", "create"), ("work", "start")]:
        descriptor = COMMAND_CATALOG.get(command_path)
        assert descriptor is not None
        assert descriptor.bootstrap_plan.auto_init_cwd is True


def test_existing_state_commands_do_not_auto_init_cwd() -> None:
    for command_path in [("spawn", "cancel"), ("config", "reset"), ("work", "list")]:
        descriptor = COMMAND_CATALOG.get(command_path)
        assert descriptor is not None
        assert descriptor.bootstrap_plan.auto_init_cwd is False


def test_deleted_chat_command_is_not_startup_classified() -> None:
    assert COMMAND_CATALOG.get(("chat",)) is None
    assert COMMAND_CATALOG.get(("chat", "ls")) is None
    assert "chat" not in COMMAND_CATALOG.top_level_names()
    assert classify_invocation(["chat", "--dry-run"], COMMAND_CATALOG) is None
