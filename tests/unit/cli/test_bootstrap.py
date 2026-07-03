import pytest

from meridian.cli.bootstrap import (
    _TOP_LEVEL_VALUE_FLAGS,  # pyright: ignore[reportPrivateUsage]
    extract_global_options,
    first_positional_token,
)


def _normalize_output_format(requested: str | None, json_mode: bool) -> str:
    if requested:
        return requested
    return "json" if json_mode else "text"


def test_extract_global_options_parses_mode_agent() -> None:
    cleaned, parsed = extract_global_options(
        ["--mode", "agent", "spawn", "list"],
        normalize_output_format=_normalize_output_format,
    )

    assert cleaned == ["spawn", "list"]
    assert parsed.forced_render_mode == "agent"


def test_extract_global_options_parses_mode_human_before_mars() -> None:
    cleaned, parsed = extract_global_options(
        ["--mode", "human", "mars", "models", "list"],
        normalize_output_format=_normalize_output_format,
    )

    assert cleaned == ["mars", "models", "list"]
    assert parsed.forced_render_mode == "human"


def test_extract_global_options_preserves_agent_profile_selection() -> None:
    cleaned, parsed = extract_global_options(
        ["--agent", "reviewer", "--dry-run"],
        normalize_output_format=_normalize_output_format,
    )

    assert cleaned == ["--agent", "reviewer", "--dry-run"]
    assert parsed.forced_render_mode is None


def test_extract_global_options_keeps_spawn_list_agent_flag_for_subcommand_validation() -> None:
    cleaned, parsed = extract_global_options(
        ["spawn", "list", "--agent", "reviewer"],
        normalize_output_format=_normalize_output_format,
    )

    assert cleaned == ["spawn", "list", "--agent", "reviewer"]
    assert parsed.forced_render_mode is None


def test_extract_global_options_rejects_conflicting_mode_values() -> None:
    with pytest.raises(SystemExit, match="conflicting --mode"):
        extract_global_options(
            ["--mode", "agent", "--mode", "human", "spawn", "list"],
            normalize_output_format=_normalize_output_format,
        )


@pytest.mark.parametrize("flag", sorted(_TOP_LEVEL_VALUE_FLAGS))
def test_root_primary_value_flags_do_not_create_command_positionals(flag: str) -> None:
    assert first_positional_token([flag, "value", "--dry-run"]) is None
