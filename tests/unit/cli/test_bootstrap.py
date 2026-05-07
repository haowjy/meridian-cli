from meridian.cli.bootstrap import extract_global_options


def _normalize_output(requested: str | None, json_mode: bool) -> str:
    if requested:
        return requested
    return "json" if json_mode else "text"


def test_chat_management_command_rejects_global_harness_flag() -> None:
    try:
        extract_global_options(
            ["--harness", "codex", "chat", "ls"],
            normalize_output_format=_normalize_output,
        )
    except SystemExit as exc:
        assert str(exc) == 'Unknown option: "--harness"'
    else:  # pragma: no cover - defensive
        raise AssertionError("expected SystemExit")


def test_chat_management_command_rejects_harness_shortcut_selector() -> None:
    try:
        extract_global_options(
            ["codex", "chat", "ls"],
            normalize_output_format=_normalize_output,
        )
    except SystemExit as exc:
        assert str(exc) == 'Unknown option: "codex"'
    else:  # pragma: no cover - defensive
        raise AssertionError("expected SystemExit")


def test_chat_launch_allows_global_harness_selector() -> None:
    cleaned, parsed = extract_global_options(
        ["--harness", "codex", "chat", "--headless"],
        normalize_output_format=_normalize_output,
    )

    assert cleaned == ["chat", "--headless"]
    assert parsed.harness == "codex"
