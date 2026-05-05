"""Resolved launch spec tests for harness adapters."""

import logging
from pathlib import Path

import pytest
from pydantic import ValidationError

from meridian.lib.core.types import ModelId
from meridian.lib.harness.adapter import SpawnParams, SubprocessHarness
from meridian.lib.harness.claude import ClaudeAdapter
from meridian.lib.harness.claude_utils import extract_session_id_from_args
from meridian.lib.harness.codex import CodexAdapter
from meridian.lib.harness.launch_spec import (
    ClaudeLaunchSpec,
    CodexLaunchSpec,
    OpenCodeLaunchSpec,
    ResolvedLaunchSpec,
)
from meridian.lib.harness.opencode import OpenCodeAdapter
from meridian.lib.harness.projections.project_opencode_subprocess import (
    HarnessCapabilityMismatch,
    project_opencode_spec_to_cli_args,
)
from meridian.lib.launch.reference import ReferenceItem
from meridian.lib.safety.permissions import (
    ApprovalMode,
    PermissionConfig,
    SandboxMode,
    TieredPermissionResolver,
)


def _resolver(
    *,
    sandbox: SandboxMode = "default",
    approval: ApprovalMode = "default",
) -> TieredPermissionResolver:
    return TieredPermissionResolver(config=PermissionConfig(sandbox=sandbox, approval=approval))


def test_claude_resolve_launch_spec_normalizes_effort_and_maps_fields(tmp_path: Path) -> None:
    resolver = _resolver()
    child_project_root = tmp_path / "child-repo"
    report_path = tmp_path / ".meridian" / "spawns" / "p123" / "report.md"
    run = SpawnParams(
        prompt="test prompt",
        model=ModelId("claude-sonnet-4-6"),
        effort="xhigh",
        agent="coder",
        adhoc_agent_payload='{"agent":"payload"}   ',
        continue_harness_session_id="  claude-session  ",
        continue_fork=True,
        appended_system_prompt="system-prompt",
        project_root=child_project_root.as_posix(),
        report_output_path=report_path.as_posix(),
    )

    spec = ClaudeAdapter().resolve_launch_spec(run, resolver)

    assert spec.model == "claude-sonnet-4-6"
    assert spec.effort == "max"
    assert spec.prompt == "test prompt"
    assert spec.continue_session_id == "claude-session"
    assert spec.continue_fork is True
    assert spec.appended_system_prompt == "system-prompt"
    assert spec.agents_payload == '{"agent":"payload"}'
    assert spec.agent_name == "coder"
    assert spec.prompt_file_path == str(report_path.parent / "system-prompt.md")
    assert spec.permission_resolver.config == resolver.config
    assert spec.permission_resolver is resolver


def test_codex_resolve_launch_spec_uses_permission_config_values() -> None:
    resolver = _resolver(sandbox="workspace-write", approval="confirm")
    run = SpawnParams(
        prompt="test prompt",
        model=ModelId("gpt-5.3-codex"),
        effort="xhigh",
        adhoc_agent_payload="base instructions",
        appended_system_prompt="developer instructions",
        user_turn_content="user turn",
    )

    spec = CodexAdapter().resolve_launch_spec(run, resolver)

    assert spec.model == "gpt-5.3-codex"
    assert spec.effort == "xhigh"
    assert spec.prompt == "user turn"
    assert spec.base_instructions == "base instructions"
    assert spec.developer_instructions == "developer instructions"
    assert spec.user_turn_content == "user turn"
    assert spec.permission_resolver.config.approval == "confirm"
    assert spec.permission_resolver.config.sandbox == "workspace-write"


def test_opencode_resolve_launch_spec_maps_fields() -> None:
    resolver = _resolver()
    run = SpawnParams(
        prompt="test prompt",
        model=ModelId("gpt-5.3-codex"),
        effort="high",
        agent="worker",
        skills=("skill-a", "skill-b"),
    )

    spec = OpenCodeAdapter().resolve_launch_spec(run, resolver)

    assert spec.model == "gpt-5.3-codex"
    assert spec.effort == "high"
    # OpenCode does not support native meridian agents; agent body goes via
    # system prompt composition, so agent_name is always None.
    assert spec.agent_name is None
    assert spec.skills == ()
    assert spec.permission_resolver.config == resolver.config


def test_opencode_resolve_launch_spec_normalizes_provider_model_once() -> None:
    resolver = _resolver()
    run = SpawnParams(
        prompt="test prompt",
        model=ModelId("openrouter/qwen/qwen3-coder:free"),
    )

    spec = OpenCodeAdapter().resolve_launch_spec(run, resolver)

    assert spec.model == "openrouter/qwen/qwen3-coder:free"


@pytest.mark.parametrize(
    ("raw_model", "expected_model"),
    (
        (ModelId("gpt-5.3-codex"), "gpt-5.3-codex"),
        (ModelId("anthropic/claude-sonnet-4-5"), "anthropic/claude-sonnet-4-5"),
        (ModelId("opencode-go/kimi-k2.6"), "opencode-go/kimi-k2.6"),
        (ModelId(""), None),
        (None, None),
    ),
)
def test_opencode_resolve_launch_spec_handles_model_shapes_exactly_once(
    raw_model: ModelId | None,
    expected_model: str | None,
) -> None:
    resolver = _resolver()
    run = SpawnParams(prompt="test prompt", model=raw_model)

    spec = OpenCodeAdapter().resolve_launch_spec(run, resolver)

    assert spec.model == expected_model


def test_opencode_subprocess_rejects_mcp_tools() -> None:
    resolver = _resolver()
    run = SpawnParams(
        prompt="test prompt",
        model=ModelId("gpt-5.3-codex"),
        mcp_tools=("tool-a=echo a",),
    )

    with pytest.raises(HarnessCapabilityMismatch, match="does not support per-spawn mcp_tools"):
        OpenCodeAdapter().build_command(run, resolver)


def test_opencode_build_command_does_not_emit_dangerous_skip_permissions() -> None:
    command = OpenCodeAdapter().build_command(
        SpawnParams(prompt="test prompt", model=ModelId("gpt-5.3-codex")),
        _resolver(approval="yolo"),
    )

    assert "--dangerously-skip-permissions" not in command


def test_opencode_subprocess_projection_logs_model_flag_collision_and_keeps_tail(
    caplog: pytest.LogCaptureFixture,
) -> None:
    spec = OpenCodeLaunchSpec(
        prompt="test prompt",
        model="anthropic/claude-sonnet-4-5",
        extra_args=("-m", "override-model"),
        permission_resolver=_resolver(),
    )

    with caplog.at_level(
        logging.DEBUG, logger="meridian.lib.harness.projections.project_opencode_subprocess"
    ):
        command = project_opencode_spec_to_cli_args(spec, base_command=("opencode", "run"))

    assert command[-3:] == ["-m", "override-model", "-"]
    assert "model" in caplog.text.lower()
    assert "extra" in caplog.text.lower()


def test_opencode_interactive_projection_omits_prompt_flag() -> None:
    """Interactive TUI launches go through managed attach; --prompt is not emitted."""
    command = project_opencode_spec_to_cli_args(
        OpenCodeLaunchSpec(
            prompt="prompt text",
            interactive=True,
            permission_resolver=_resolver(),
        ),
        base_command=("opencode",),
    )

    assert command == ["opencode"]
    assert "--prompt" not in command


def test_opencode_subprocess_projection_does_not_emit_file_injection_flags() -> None:
    spec = OpenCodeLaunchSpec(
        prompt="test prompt",
        permission_resolver=_resolver(),
        reference_items=(
            ReferenceItem(
                kind="file",
                path=Path("/repo/src/auth.py"),
                body="print('ok')",
            ),
            ReferenceItem(
                kind="directory",
                path=Path("/repo/src"),
                body="tree",
            ),
            ReferenceItem(
                kind="file",
                path=Path("/repo/src/binary.dat"),
                body="",
                warning="Binary file: 10KB",
            ),
            ReferenceItem(
                kind="file",
                path=Path("/repo/src/empty.py"),
                body="",
            ),
        ),
    )

    command = project_opencode_spec_to_cli_args(spec, base_command=("opencode", "run"))

    assert "--file" not in command
    assert "/repo/src/auth.py" not in command
    assert "/repo/src/binary.dat" not in command
    assert "/repo/src/empty.py" not in command
    assert command[-1] == "-"


@pytest.mark.parametrize(
    "adapter",
    (
        ClaudeAdapter(),
        CodexAdapter(),
        OpenCodeAdapter(),
    ),
)
def test_resolve_launch_spec_keeps_none_effort(adapter: SubprocessHarness) -> None:
    resolver = _resolver()
    run = SpawnParams(prompt="test prompt", effort=None)

    spec = adapter.resolve_launch_spec(run, resolver)

    assert spec.effort is None


@pytest.mark.parametrize(
    "spec_cls",
    (
        ClaudeLaunchSpec,
        CodexLaunchSpec,
        OpenCodeLaunchSpec,
    ),
)
def test_continue_fork_requires_continue_session_id(spec_cls: type[ResolvedLaunchSpec]) -> None:
    with pytest.raises(ValidationError) as exc_info:
        spec_cls(
            prompt="test",
            continue_fork=True,
            permission_resolver=_resolver(),
        )

    assert "continue_fork=True requires continue_session_id" in str(exc_info.value)
    errors = exc_info.value.errors()
    assert len(errors) == 1
    assert errors[0]["type"] == "value_error"
    ctx = errors[0].get("ctx")
    assert isinstance(ctx, dict)
    underlying = ctx.get("error")
    assert isinstance(underlying, ValueError)
    assert str(underlying) == "continue_fork=True requires continue_session_id"


@pytest.mark.parametrize(
    ("spec_cls", "continue_session_id", "continue_fork"),
    (
        (ClaudeLaunchSpec, None, False),
        (ClaudeLaunchSpec, "claude-session", True),
        (CodexLaunchSpec, None, False),
        (CodexLaunchSpec, "codex-session", True),
        (OpenCodeLaunchSpec, None, False),
        (OpenCodeLaunchSpec, "opencode-session", True),
    ),
)
def test_continue_fork_valid_combinations_pass(
    spec_cls: type[ResolvedLaunchSpec],
    continue_session_id: str | None,
    continue_fork: bool,
) -> None:
    spec = spec_cls(
        prompt="test",
        continue_session_id=continue_session_id,
        continue_fork=continue_fork,
        permission_resolver=_resolver(),
    )

    assert spec.continue_session_id == continue_session_id
    assert spec.continue_fork is continue_fork


def test_fresh_child_spawn_gets_seeded_session_id() -> None:
    spec = ClaudeAdapter().resolve_launch_spec(
        SpawnParams(prompt="test prompt"),
        _resolver(),
    )

    seeded_id = extract_session_id_from_args(spec.extra_args)

    assert seeded_id is not None
    assert spec.extra_args[-2:] == ("--session-id", seeded_id)


def test_continuation_spawn_no_seed() -> None:
    spec = ClaudeAdapter().resolve_launch_spec(
        SpawnParams(
            prompt="test prompt",
            continue_harness_session_id="claude-session",
        ),
        _resolver(),
    )

    assert spec.continue_session_id == "claude-session"
    assert extract_session_id_from_args(spec.extra_args) is None


def test_user_supplied_session_id_not_duplicated() -> None:
    spec = ClaudeAdapter().resolve_launch_spec(
        SpawnParams(
            prompt="test prompt",
            extra_args=("--session-id", "user-session"),
        ),
        _resolver(),
    )

    assert spec.extra_args == ("--session-id", "user-session")
    assert extract_session_id_from_args(spec.extra_args) == "user-session"




@pytest.mark.parametrize(
    "extra_args",
    (
        ("--resume", "abc"),
        ("--resume=abc",),
        ("-r", "abc"),
        ("-c",),
        ("--continue",),
        ("--continue=abc",),
        ("--session-id", "xyz"),
    ),
)
def test_claude_passthrough_session_identity_flags_are_not_seeded(
    extra_args: tuple[str, ...],
) -> None:
    spec = ClaudeAdapter().resolve_launch_spec(
        SpawnParams(prompt="test prompt", extra_args=extra_args),
        _resolver(),
    )

    assert spec.extra_args == extra_args

def test_interactive_spawn_no_seed() -> None:
    spec = ClaudeAdapter().resolve_launch_spec(
        SpawnParams(prompt="test prompt", interactive=True),
        _resolver(),
    )

    assert extract_session_id_from_args(spec.extra_args) is None

def test_opencode_explicit_harness_allows_raw_provider_model() -> None:
    """When harness is explicit, raw provider/model IDs pass through unchanged."""
    resolver = _resolver()
    run = SpawnParams(
        prompt="test prompt",
        model=ModelId("opencode-go/kimi-k2.6"),
        harness="opencode",
    )

    spec = OpenCodeAdapter().resolve_launch_spec(run, resolver)
    assert spec.model == "opencode-go/kimi-k2.6"

    command = OpenCodeAdapter().build_command(run, resolver)
    assert "opencode-go/kimi-k2.6" in command
