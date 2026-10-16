"""OpenCode subprocess projection is version-aware on resume-with-model.

``opencode run --session`` cannot switch the model committed to a resumed
session on V1, so the projector rejects that shape loudly for a known V1 —
matching the V1 streaming contract in ``connections/opencode_http.py``. V2
supports the switch (``POST /api/session/{id}/model``) and keeps ``--model``.
An unresolved version forwards rather than rejecting: the raw CLI accepts the
flag, and only a known V1 is grounds for failure.
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


def test_v1_subprocess_resume_with_explicit_model_fails_loudly() -> None:
    with pytest.raises(HarnessCapabilityMismatch, match="cannot switch the model"):
        project_opencode_spec_to_cli_args(
            _spec(
                continue_session_id="ses-parent",
                model="openai/gpt-5.5",
                opencode_version="v1",
            ),
            base_command=_SUBPROCESS_BASE,
        )


@pytest.mark.parametrize("version", ["v2", None])
def test_subprocess_resume_forwards_model_unless_version_is_v1(
    version: str | None,
) -> None:
    overrides: dict[str, object] = {
        "continue_session_id": "ses-parent",
        "model": "openai/gpt-5.5",
    }
    if version is not None:
        overrides["opencode_version"] = version
    command = project_opencode_spec_to_cli_args(
        _spec(**overrides),
        base_command=_SUBPROCESS_BASE,
    )

    assert command[command.index("--model") + 1] == "openai/gpt-5.5"
    assert command[command.index("--session") + 1] == "ses-parent"


def test_interactive_resume_keeps_model_for_managed_attach() -> None:
    # Interactive primary continues go through managed attach; the version-aware
    # streaming guard is the enforcement point, so the argv fallback preview may
    # still carry ``--model`` and must not be rejected here.
    command = project_opencode_spec_to_cli_args(
        _spec(
            continue_session_id="ses-parent",
            model="openai/gpt-5.5",
            interactive=True,
            opencode_version="v1",
        ),
        base_command=_INTERACTIVE_BASE,
    )

    assert command[command.index("--model") + 1] == "openai/gpt-5.5"
    assert command[command.index("--session") + 1] == "ses-parent"
    assert command[-2:] == ["--prompt", "hello"]
