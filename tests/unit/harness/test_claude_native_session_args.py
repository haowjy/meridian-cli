"""Bounded Claude primary passthrough grammar."""

import pytest

from meridian.lib.harness.claude import normalize_primary_session_args
from meridian.lib.harness.native_session_args import (
    NativeSessionSelector,
    NormalizedNativeSessionArgs,
)

_NATIVE_ID = "550e8400-e29b-41d4-a716-446655440000"


@pytest.mark.parametrize(
    "selector_args",
    [("--resume", _NATIVE_ID), (f"--resume={_NATIVE_ID}",), ("-r", _NATIVE_ID)],
)
@pytest.mark.parametrize("fork_args", [(), ("--fork-session",)])
def test_normalizes_claude_primary_selectors_and_preserves_safe_tail(
    selector_args: tuple[str, ...], fork_args: tuple[str, ...]
) -> None:
    raw = (*selector_args, "--model", "resume A", *fork_args, "--add-dir=/safe/path")
    operation = "fork" if fork_args else "resume"
    assert normalize_primary_session_args(raw) == NormalizedNativeSessionArgs(
        NativeSessionSelector(operation, _NATIVE_ID),
        ("--model", "resume A", "--add-dir=/safe/path"),
    )


def test_claude_primary_keeps_safe_args_exactly_without_selector() -> None:
    raw = ("--system-prompt=resume A", "--dangerously-skip-permissions", "--add-dir", "/tmp/a")
    assert normalize_primary_session_args(raw).remaining_args == raw


@pytest.mark.parametrize(
    "raw",
    [
        ("--system-prompt=--resume A",),
        ("--append-system-prompt=--resume A",),
    ],
)
def test_equals_prompt_values_may_start_with_option_spelling(raw: tuple[str, ...]) -> None:
    assert normalize_primary_session_args(raw).remaining_args == raw


@pytest.mark.parametrize("option", ["--system-prompt", "--append-system-prompt"])
def test_separated_prompt_values_that_look_like_flags_remain_refused(option: str) -> None:
    with pytest.raises(ValueError, match="requires an unambiguous value"):
        normalize_primary_session_args((option, "--resume A"))


@pytest.mark.parametrize(
    "raw",
    [
        ("--resume", "latest"),
        ("--resume", "a conversation title"),
        ("--resume", "@session.json"),
        ("--resume", "/tmp/session.json"),
        ("--resume", "550e8400-e29b-41d4-a716-44665544000A"),
        ("--resume", " 550e8400-e29b-41d4-a716-446655440000"),
        ("--resume=latest",),
        ("-r", "@session.json"),
    ],
)
def test_claude_raw_resume_requires_canonical_native_uuid(raw: tuple[str, ...]) -> None:
    with pytest.raises(ValueError, match="canonical native UUID"):
        normalize_primary_session_args(raw)


@pytest.mark.parametrize(
    ("raw", "visible"),
    [
        (("--unknown=synthetic-secret-value",), "--unknown"),
        (("--settings=synthetic-secret-value",), "--settings"),
        (("--profile", "synthetic-secret-value"), "--profile"),
        (("--bad name=synthetic-secret-value",), ""),
    ],
)
def test_unknown_option_errors_name_only_safe_option(
    raw: tuple[str, ...], visible: str
) -> None:
    with pytest.raises(ValueError) as error:
        normalize_primary_session_args(raw)
    if visible:
        assert visible in str(error.value)
    assert "synthetic-secret-value" not in str(error.value)
    if visible in {"--settings", "--profile"}:
        assert "typed settings/profile configuration" in str(error.value)


@pytest.mark.parametrize(
    "raw",
    [
        ("--resume",),
        ("-r",),
        ("--resume", " "),
        ("--resume", "--model"),
        ("--resume", "c12"),
        ("--resume", "p4"),
        ("--resume", "A", "--resume", "A"),
        ("--resume", "A", "--fork-session", "--fork-session"),
        ("--fork-session",),
        ("--continue",),
        ("-c",),
        ("--session-id", "A"),
        ("--resume", "A", "--session-id", "U"),
        ("--", "--resume", "A"),
        ("@args.txt",),
        ("prompt",),
        ("--unknown",),
        ("--model",),
        ("--model", "--resume"),
        ("--resume=A", "-r", "A"),
    ],
)
def test_refuses_ambiguous_or_unbounded_claude_primary_args(raw: tuple[str, ...]) -> None:
    with pytest.raises(ValueError):
        normalize_primary_session_args(raw)


def test_claude_primary_managed_surface_is_unsupported() -> None:
    with pytest.raises(ValueError, match="managed"):
        normalize_primary_session_args((), surface="managed")


def test_generated_claude_resume_projection_keeps_identity_separate_from_raw_tail() -> None:
    from meridian.lib.harness.adapter import SpawnParams
    from meridian.lib.harness.claude import ClaudeAdapter
    from meridian.lib.harness.projections.project_claude import project_claude_spec_to_cli_args
    from meridian.lib.safety.permissions import UnsafeNoOpPermissionResolver

    spec = ClaudeAdapter().resolve_launch_spec(
        SpawnParams(
            prompt="hello",
            continue_harness_session_id=_NATIVE_ID,
            extra_args=("--model", "sonnet"),
        ),
        UnsafeNoOpPermissionResolver(_suppress_warning=True),
    )
    argv = project_claude_spec_to_cli_args(spec, base_command=("claude",))
    assert argv.count("--resume") == 1
    assert argv[argv.index("--resume") + 1] == _NATIVE_ID
    assert "--session-id" not in argv


def test_generated_claude_fresh_projection_keeps_create_target_distinct() -> None:
    from meridian.lib.harness.adapter import SpawnParams
    from meridian.lib.harness.claude import ClaudeAdapter
    from meridian.lib.harness.projections.project_claude import project_claude_spec_to_cli_args
    from meridian.lib.safety.permissions import UnsafeNoOpPermissionResolver

    spec = ClaudeAdapter().resolve_launch_spec(
        SpawnParams(prompt="hello"),
        UnsafeNoOpPermissionResolver(_suppress_warning=True),
    )
    argv = project_claude_spec_to_cli_args(spec, base_command=("claude",))
    assert argv.count("--session-id") == 1
    assert argv[argv.index("--session-id") + 1] == spec.extra_args[-1]
    assert "--resume" not in argv
