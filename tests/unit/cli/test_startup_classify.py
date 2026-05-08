"""Classifier contract tests for startup-cheap CLI dispatch."""

from meridian.cli.bootstrap import first_positional_token_with_index
from meridian.cli.startup.catalog import COMMAND_CATALOG
from meridian.cli.startup.classify import classify_invocation
from meridian.cli.startup.policy import RootSource


def _path(argv: list[str]) -> tuple[str, ...] | None:
    descriptor = classify_invocation(argv, COMMAND_CATALOG)
    if descriptor is None:
        return None
    return descriptor.command_path


def test_root_help_is_not_classified_as_a_command() -> None:
    assert classify_invocation(["--help"], COMMAND_CATALOG) is None


def test_longest_prefix_matching_prefers_deep_spawn_report_show_path() -> None:
    assert _path(["spawn", "report", "show", "p1"]) == ("spawn", "report", "show")


def test_spawn_defaults_to_create_descriptor_after_flag_stripping() -> None:
    assert _path(["--format", "json", "spawn", "-m", "gpt", "-p", "hello"]) == ("spawn",)


def test_harness_shortcut_is_removed_before_classification() -> None:
    assert _path(["codex", "spawn", "list"]) == ("spawn", "list")


def test_unknown_flags_do_not_consume_following_command_tokens() -> None:
    assert _path(["--bogus", "spawn", "list"]) == ("spawn", "list")


def test_bootstrap_positional_scan_does_not_consume_after_unknown_flags() -> None:
    assert first_positional_token_with_index(["--bogus", "spawn", "list"]) == (1, "spawn")


def test_passthrough_tokens_do_not_affect_classification() -> None:
    assert _path(["spawn", "create", "--", "--add-dir", "/tmp/project"]) == ("spawn", "create")


def test_models_list_descriptor_owns_redirect_policy() -> None:
    descriptor = classify_invocation(["models", "list"], COMMAND_CATALOG)

    assert descriptor is not None
    assert descriptor.command_path == ("models", "list")
    assert descriptor.redirect is not None
    assert descriptor.redirect.target == "meridian mars models list"


def test_init_descriptor_uses_argument_root_source() -> None:
    descriptor = COMMAND_CATALOG.get(("init",))

    assert descriptor is not None
    assert descriptor.root_source is RootSource.ARGV


def test_unknown_command_is_not_classified_as_root() -> None:
    assert classify_invocation(["does-not-exist"], COMMAND_CATALOG) is None
