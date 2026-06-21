from meridian.cli.startup.catalog import COMMAND_CATALOG
from meridian.cli.startup.classify import classify_invocation


def test_deleted_chat_command_is_not_startup_classified() -> None:
    assert COMMAND_CATALOG.get(("chat",)) is None
    assert COMMAND_CATALOG.get(("chat", "ls")) is None
    assert "chat" not in COMMAND_CATALOG.top_level_names()
    assert classify_invocation(["chat", "--dry-run"], COMMAND_CATALOG) is None
