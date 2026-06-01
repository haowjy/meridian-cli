import inspect
from typing import Annotated, get_args, get_origin

import pytest
from cyclopts import Parameter

from meridian.cli.bootstrap import (
    _TOP_LEVEL_BOOL_FLAGS,  # pyright: ignore[reportPrivateUsage]
    extract_global_options,
    first_positional_token,
)


def _normalize_output_format(requested: str | None, json_mode: bool) -> str:
    if requested:
        return requested
    return "json" if json_mode else "text"


def test_extract_global_options_keeps_spawn_list_command_after_force_agent_flag() -> None:
    cleaned, parsed = extract_global_options(
        ["--agent", "spawn", "list"],
        normalize_output_format=_normalize_output_format,
    )

    assert cleaned == ["spawn", "list"]
    assert parsed.force_agent is True


def test_extract_global_options_keeps_mars_command_after_force_agent_flag() -> None:
    cleaned, parsed = extract_global_options(
        ["--agent", "mars", "models", "list"],
        normalize_output_format=_normalize_output_format,
    )

    assert cleaned == ["mars", "models", "list"]
    assert parsed.force_agent is True


def test_extract_global_options_preserves_agent_profile_selection() -> None:
    cleaned, parsed = extract_global_options(
        ["--agent", "reviewer", "--dry-run"],
        normalize_output_format=_normalize_output_format,
    )

    assert cleaned == ["-a", "reviewer", "--dry-run"]
    assert parsed.force_agent is False


def test_extract_global_options_keeps_spawn_list_agent_flag_for_subcommand_validation() -> None:
    cleaned, parsed = extract_global_options(
        ["spawn", "list", "--agent", "reviewer"],
        normalize_output_format=_normalize_output_format,
    )

    assert cleaned == ["spawn", "list", "--agent", "reviewer"]
    assert parsed.force_agent is False


def _root_value_flags_from_signature() -> tuple[str, ...]:
    from meridian.cli.main import root

    flags: set[str] = set()
    for param in inspect.signature(root).parameters.values():
        annotation = param.annotation
        if get_origin(annotation) is not Annotated:
            continue
        value_type, *metadata = get_args(annotation)
        if value_type is bool:
            continue
        for item in metadata:
            if isinstance(item, Parameter):
                flags.update(name for name in item.name if name not in _TOP_LEVEL_BOOL_FLAGS)
    return tuple(sorted(flags))


@pytest.mark.parametrize("flag", _root_value_flags_from_signature())
def test_root_primary_value_flags_do_not_create_command_positionals(flag: str) -> None:
    assert first_positional_token([flag, "value", "--dry-run"]) is None
