"""Cursor subprocess projection tests."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from meridian.lib.harness.projections.project_cursor import (
    HarnessCapabilityMismatch,
    _resolve_cursor_model,
    project_cursor_spec_to_cli_args,
)
from meridian.lib.launch.constants import BASE_COMMAND_CURSOR_SUBPROCESS
from meridian.lib.launch.launch_types import PermissionResolver, ResolvedLaunchSpec
from meridian.lib.safety.permissions import ApprovalMode, PermissionConfig


class _Resolver(PermissionResolver):
    def __init__(self, *, approval: str) -> None:
        self._config = PermissionConfig(approval=cast("ApprovalMode", approval))

    @property
    def config(self) -> PermissionConfig:
        return self._config

    def resolve_flags(self) -> tuple[str, ...]:
        return ("--dangerously-bypass-approvals-and-sandbox",)


@pytest.mark.parametrize(
    ("approval", "expected_flag"),
    [
        ("default", None),
        ("confirm", None),
        ("auto", "--force"),
        ("yolo", "--yolo"),
    ],
)
def test_cursor_projection_maps_approval_flags_and_keeps_prompt_last(
    approval: str,
    expected_flag: str | None,
    tmp_path: Path,
) -> None:
    task_cwd = str(tmp_path / "task-cwd")
    spec = ResolvedLaunchSpec(
        harness="cursor",
        model="composer-2.5",
        prompt="Reply with exactly OK",
        permission_resolver=_Resolver(approval=approval),
        task_cwd=task_cwd,
        extra_args=("--foo", "bar"),
    )

    command = project_cursor_spec_to_cli_args(spec, base_command=BASE_COMMAND_CURSOR_SUBPROCESS)

    assert command[:5] == ["cursor", "agent", "--print", "--output-format", "stream-json"]
    assert command[5] == "--trust"
    assert command[command.index("--model") + 1] == "composer-2.5"
    assert command[command.index("--workspace") + 1] == task_cwd
    if expected_flag is None:
        assert "--force" not in command
        assert "--yolo" not in command
    else:
        assert expected_flag in command
    # Projection must ignore resolver-provided shared flags.
    assert "--dangerously-bypass-approvals-and-sandbox" not in command
    assert command[-3:] == ["--foo", "bar", "Reply with exactly OK"]


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"continue_session_id": "ses-123"}, "session resume"),
        ({"continue_session_id": "ses-123", "continue_fork": True}, "continue_fork"),
        ({"mcp_tools": ("fs",)}, "mcp_tools"),
        ({"interactive": True}, "interactive mode"),
    ],
)
def test_cursor_projection_rejects_mvp_unsupported_fields(
    updates: dict[str, object],
    message: str,
) -> None:
    base_spec = ResolvedLaunchSpec(
        harness="cursor",
        prompt="hello",
        permission_resolver=_Resolver(approval="default"),
    )

    with pytest.raises(HarnessCapabilityMismatch, match=message):
        project_cursor_spec_to_cli_args(
            base_spec.model_copy(update=updates),
            base_command=BASE_COMMAND_CURSOR_SUBPROCESS,
        )


def test_cursor_projection_ignores_projected_roots_for_mvp(tmp_path: Path) -> None:
    task_cwd = str(tmp_path / "task-cwd")
    root_a = tmp_path / "root-a"
    root_b = tmp_path / "root-b"
    spec = ResolvedLaunchSpec(
        harness="cursor",
        prompt="hello",
        task_cwd=task_cwd,
        projected_roots=(root_a, root_b),
        permission_resolver=_Resolver(approval="default"),
    )

    command = project_cursor_spec_to_cli_args(spec, base_command=BASE_COMMAND_CURSOR_SUBPROCESS)

    assert command[command.index("--workspace") + 1] == task_cwd
    assert str(root_a) not in command
    assert str(root_b) not in command

# ---------------------------------------------------------------------------
# _resolve_cursor_model algorithm tests
# ---------------------------------------------------------------------------


class TestResolveCursorModel:
    """Unit tests for the cursor model-slug resolution algorithm."""

    # Rule 1: exact catalog match with no effort → passthrough verbatim
    def test_exact_match_no_effort_passthrough(self) -> None:
        result = _resolve_cursor_model("gpt-5.5", None, ["gpt-5.5", "gpt-5.5-high"])
        assert result == "gpt-5.5"

    # Rule 2: no effort, model not in catalog → return model unchanged
    def test_no_effort_no_catalog_match_returns_model(self) -> None:
        result = _resolve_cursor_model("gpt-5.5", None, [])
        assert result == "gpt-5.5"

    # Rule 4: no matching candidates → fallback construction
    def test_fallback_construction_when_no_candidates(self) -> None:
        result = _resolve_cursor_model("gpt-5.5", "high", [])
        assert result == "gpt-5.5-high"

    def test_fallback_construction_empty_effort_stripped(self) -> None:
        result = _resolve_cursor_model("gpt-5.5", "  ", [])
        assert result == "gpt-5.5"

    # Boundary-aware prefix: model='gpt-5' must NOT match 'gpt-5.1-high'
    def test_prefix_boundary_no_dot_match(self) -> None:
        """gpt-5 should not match gpt-5.1-high (dot prevents prefix match)."""
        result = _resolve_cursor_model("gpt-5", "high", ["gpt-5.1-high", "gpt-5.2-high"])
        assert result == "gpt-5-high"  # fallback; no valid prefix match

    def test_prefix_boundary_exact_prefix_matches(self) -> None:
        """gpt-5.5 should match gpt-5.5-high (correct prefix)."""
        result = _resolve_cursor_model("gpt-5.5", "high", ["gpt-5.5-high", "gpt-5.5-low"])
        assert result == "gpt-5.5-high"

    # Effort substring: effort='high' should prefer 'gpt-5.5-high' over 'gpt-5.5-extra-high'
    def test_effort_substring_prefers_shorter_match(self) -> None:
        """When both gpt-5.5-high and gpt-5.5-extra-high match effort='high',
        pick the shorter slug (gpt-5.5-high)."""
        result = _resolve_cursor_model(
            "gpt-5.5", "high", ["gpt-5.5-extra-high", "gpt-5.5-high", "gpt-5.5-low"]
        )
        assert result == "gpt-5.5-high"

    # D5: thinking variant preference
    def test_thinking_variant_preferred_over_plain(self) -> None:
        result = _resolve_cursor_model(
            "claude-opus-4-7",
            "high",
            ["claude-opus-4-7-high", "claude-opus-4-7-thinking-high"],
        )
        assert result == "claude-opus-4-7-thinking-high"

    def test_thinking_before_effort_matches(self) -> None:
        """Slugs like model-thinking-effort are matched by effort."""
        result = _resolve_cursor_model(
            "claude-opus-4-7",
            "high",
            ["claude-opus-4-7-thinking-high"],
        )
        assert result == "claude-opus-4-7-thinking-high"

    def test_thinking_variant_min_length_tiebreak(self) -> None:
        """Multiple thinking variants → shortest wins."""
        result = _resolve_cursor_model(
            "claude-opus-4-7",
            "high",
            [
                "claude-opus-4-7-thinking-high",
                "claude-opus-4-7-thinking-high-v2",
            ],
        )
        assert result == "claude-opus-4-7-thinking-high"

    # Single exact candidate
    def test_single_effort_candidate_returned(self) -> None:
        result = _resolve_cursor_model("gpt-5.5", "low", ["gpt-5.5-high", "gpt-5.5-low"])
        assert result == "gpt-5.5-low"

    # Effort normalisation (case, whitespace)
    def test_effort_normalized_lowercase(self) -> None:
        result = _resolve_cursor_model("gpt-5.5", "HIGH", ["gpt-5.5-high"])
        assert result == "gpt-5.5-high"

    def test_effort_normalized_strip_whitespace(self) -> None:
        result = _resolve_cursor_model("gpt-5.5", "  high  ", ["gpt-5.5-high"])
        assert result == "gpt-5.5-high"

    # Edge case: effort='high' with only 'extra-high' in catalog
    def test_effort_high_matches_extra_high_when_no_plain_high(self) -> None:
        """When effort='high' and only 'extra-high' exists, it matches via endswith."""
        result = _resolve_cursor_model("gpt-5.5", "high", ["gpt-5.5-extra-high"])
        assert result == "gpt-5.5-extra-high"
