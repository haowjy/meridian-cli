"""Bounded Claude primary passthrough grammar."""

import pytest

from meridian.lib.harness.claude import normalize_primary_session_args
from meridian.lib.harness.native_session_args import (
    NativeSessionSelector,
    NormalizedNativeSessionArgs,
)


@pytest.mark.parametrize(
    "selector_args",
    [("--resume", "native-A"), ("--resume=native-A",), ("-r", "native-A")],
)
@pytest.mark.parametrize("fork_args", [(), ("--fork-session",)])
def test_normalizes_claude_primary_selectors_and_preserves_safe_tail(
    selector_args: tuple[str, ...], fork_args: tuple[str, ...]
) -> None:
    raw = (*selector_args, "--model", "resume A", *fork_args, "--add-dir=/safe/path")
    operation = "fork" if fork_args else "resume"
    assert normalize_primary_session_args(raw) == NormalizedNativeSessionArgs(
        NativeSessionSelector(operation, "native-A"),
        ("--model", "resume A", "--add-dir=/safe/path"),
    )


def test_claude_primary_keeps_safe_args_exactly_without_selector() -> None:
    raw = ("--system-prompt=resume A", "--dangerously-skip-permissions", "--add-dir", "/tmp/a")
    assert normalize_primary_session_args(raw).remaining_args == raw


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
            continue_harness_session_id="native-A",
            extra_args=("--model", "sonnet"),
        ),
        UnsafeNoOpPermissionResolver(_suppress_warning=True),
    )
    argv = project_claude_spec_to_cli_args(spec, base_command=("claude",))
    assert argv.count("--resume") == 1
    assert argv[argv.index("--resume") + 1] == "native-A"
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
