"""Bounded Claude primary passthrough grammar."""

import pytest

from meridian.lib.core.execution_policy import ResolvedExecutionPolicy
from meridian.lib.harness.claude import normalize_primary_session_args
from meridian.lib.harness.native_session_args import (
    NativeSessionSelector,
    NormalizedNativeSessionArgs,
    PrimaryArgControls,
)

_NATIVE_ID = "550e8400-e29b-41d4-a716-446655440000"


def _controls(*, model: str | None = "sonnet", controlled: bool = True, effort: str | None = None):
    return PrimaryArgControls(
        model=model,
        model_controlled=controlled,
        execution_policy=ResolvedExecutionPolicy(effort=effort),
    )


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


@pytest.mark.parametrize("raw", [("--model", "sonnet"), ("--model=attacker-model",)])
def test_controls_strip_equal_or_different_generated_model_with_redacted_warning(
    raw: tuple[str, ...],
) -> None:
    result = normalize_primary_session_args(raw, controls=_controls())
    assert result.remaining_args == ()
    assert result.warnings == (
        "Ignored raw model option; Meridian's resolved model takes precedence.",
    )
    assert "attacker-model" not in str(result.warnings)


def test_controlled_default_model_strips_raw_model_because_projector_omits_it() -> None:
    result = normalize_primary_session_args(
        ("--model=raw-model",), controls=_controls(model=None)
    )
    assert result.remaining_args == ()
    assert len(result.warnings) == 1


def test_claude_projector_owns_nonblank_model_and_effort_and_omits_default_model() -> None:
    from meridian.lib.harness.adapter import SpawnParams
    from meridian.lib.harness.claude import ClaudeAdapter
    from meridian.lib.harness.projections.project_claude import project_claude_spec_to_cli_args
    from meridian.lib.safety.permissions import UnsafeNoOpPermissionResolver

    spec = ClaudeAdapter().resolve_launch_spec(
        SpawnParams(prompt="safe"), UnsafeNoOpPermissionResolver(_suppress_warning=True)
    ).model_copy(update={"model": "", "effort": "high"})
    argv = project_claude_spec_to_cli_args(spec, base_command=("claude",))
    assert "--model" not in argv
    assert argv[argv.index("--effort") + 1] == "high"


def test_unknown_model_provenance_refuses_scalar_but_benign_prompt_is_valid() -> None:
    with pytest.raises(ValueError, match="unknown Meridian provenance"):
        normalize_primary_session_args(
            ("--model", "secret-model"), controls=_controls(controlled=False)
        )
    result = normalize_primary_session_args(
        ("--system-prompt=--model",), controls=_controls(controlled=False)
    )
    assert result.remaining_args == ("--system-prompt=--model",)


def test_controls_parse_roles_once_and_refuse_repeated_raw_model() -> None:
    with pytest.raises(ValueError, match="repeat the model"):
        normalize_primary_session_args(
            ("--model", "first", "--model=second"), controls=_controls()
        )
    # Inline values are consumed as values, never reinterpreted as option names.
    result = normalize_primary_session_args(
        ("--model=--effort", "--append-system-prompt=--model"), controls=_controls()
    )
    assert result.remaining_args == ("--append-system-prompt=--model",)


@pytest.mark.parametrize("raw", [("--effort", "high"), ("--effort=low",)])
def test_supported_emitted_effort_strips_equal_or_different_duplicate(
    raw: tuple[str, ...],
) -> None:
    for retained_effort in ("low", "medium", "high", "xhigh", "max"):
        result = normalize_primary_session_args(raw, controls=_controls(effort=retained_effort))
        assert result.remaining_args == ()
        assert result.warnings == (
            "Ignored raw effort option; Meridian's resolved effort takes precedence.",
        )


@pytest.mark.parametrize("effort", [None, "", "default"])
def test_absent_or_unemitted_effort_refuses_raw_scalar(effort: str | None) -> None:
    with pytest.raises(ValueError, match="no emitted Meridian effort"):
        normalize_primary_session_args(("--effort", "high"), controls=_controls(effort=effort))
    for unsupported in ("future-secret-effort", "HIGH", "--permission-mode"):
        with pytest.raises(ValueError) as error:
            normalize_primary_session_args(
                ("--effort=low",), controls=_controls(effort=unsupported)
            )
        assert str(error.value) == (
            "Claude raw effort option has unsupported Meridian effort; "
            "use Meridian's effort configuration"
        )
        assert unsupported not in str(error.value)


def test_permission_and_bypass_controls_refuse_before_other_scalar_suppression() -> None:
    with pytest.raises(ValueError, match="permission mode"):
        normalize_primary_session_args(
            ("--model", "sonnet", "--permission-mode", "default"), controls=_controls()
        )
    with pytest.raises(ValueError, match="permission bypass"):
        normalize_primary_session_args(("--dangerously-skip-permissions",), controls=_controls())


def test_tool_lists_keep_additive_projection_but_refuse_mandatory_agent_denial() -> None:
    raw = ("--allowedTools=Read,Write", "--disallowedTools", "Bash")
    assert normalize_primary_session_args(raw, controls=_controls()).remaining_args == raw
    with pytest.raises(ValueError, match="mandatory Agent denial"):
        normalize_primary_session_args(
            ("--allowedTools=Read,Agent(Explore)",), controls=_controls()
        )


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
