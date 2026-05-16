from meridian.cli.bootstrap import extract_global_options


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
