"""OpenCode subprocess projection: resume-with-model is rejected loudly.

R11: the non-interactive ``opencode run`` subprocess transport cannot switch the
model committed to a resumed session. It fails loudly rather than forwarding
``--model`` (which the transport does not honor) or silently dropping it, matching
the V1 streaming contract in ``connections/opencode_http.py``.
"""

from __future__ import annotations

import pytest

from meridian.lib.harness.projections.project_opencode_subprocess import (
    project_opencode_spec_to_cli_args,
)
from meridian.lib.harness.projections.projection_errors import HarnessCapabilityMismatch
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.safety.permissions import UnsafeNoOpPermissionResolver

_SUBPROCESS_BASE = ("opencode", "run")
_INTERACTIVE_BASE = ("opencode",)


def _spec(**overrides: object) -> ResolvedLaunchSpec:
    values: dict[str, object] = {
        "prompt": "hello",
        "permission_resolver": UnsafeNoOpPermissionResolver(_suppress_warning=True),
    }
    values.update(overrides)
    return ResolvedLaunchSpec(**values)


def test_subprocess_resume_with_explicit_model_fails_loudly() -> None:
    with pytest.raises(HarnessCapabilityMismatch, match="cannot switch the model"):
        project_opencode_spec_to_cli_args(
            _spec(continue_session_id="ses-parent", model="openai/gpt-5.5"),
            base_command=_SUBPROCESS_BASE,
        )


def test_subprocess_resume_without_model_forwards_session_only() -> None:
    command = project_opencode_spec_to_cli_args(
        _spec(continue_session_id="ses-parent"),
        base_command=_SUBPROCESS_BASE,
    )

    assert "--session" in command
    assert command[command.index("--session") + 1] == "ses-parent"
    assert "--model" not in command


def test_fresh_subprocess_launch_still_forwards_model() -> None:
    command = project_opencode_spec_to_cli_args(
        _spec(model="openai/gpt-5.5"),
        base_command=_SUBPROCESS_BASE,
    )

    assert command[command.index("--model") + 1] == "openai/gpt-5.5"
    assert "--session" not in command


def test_interactive_resume_keeps_model_for_managed_attach() -> None:
    # Interactive primary continues go through managed attach; the version-aware
    # streaming guard is the enforcement point, so the argv fallback preview may
    # still carry ``--model`` and must not be rejected here.
    command = project_opencode_spec_to_cli_args(
        _spec(continue_session_id="ses-parent", model="openai/gpt-5.5", interactive=True),
        base_command=_INTERACTIVE_BASE,
    )

    assert command[command.index("--model") + 1] == "openai/gpt-5.5"
    assert command[command.index("--session") + 1] == "ses-parent"
