"""OpenCode adapter drops a replayed model on exact continue.

Exact continue replays the session's own model, and OpenCode resume keeps the
committed model, so a replayed token is not a switch request. Dropping it leaves
an explicit ``--model`` override as the only model that reaches the V1 resume
guard (V1 cannot change a session's model).
"""

from __future__ import annotations

from meridian.lib.harness.adapter import SpawnParams
from meridian.lib.harness.opencode import OpenCodeAdapter
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.safety.permissions import UnsafeNoOpPermissionResolver


def _resolve(**overrides: object) -> ResolvedLaunchSpec:
    params: dict[str, object] = {"prompt": "hello", "model": "deepseek/deepseek-flash"}
    params.update(overrides)
    return OpenCodeAdapter().resolve_launch_spec(
        SpawnParams(**params),
        UnsafeNoOpPermissionResolver(_suppress_warning=True),
    )


def test_exact_continue_replay_drops_model() -> None:
    spec = _resolve(continue_harness_session_id="ses-parent")

    assert spec.model is None


def test_explicit_override_keeps_model_on_continue() -> None:
    spec = _resolve(
        continue_harness_session_id="ses-parent",
        model_override_explicit=True,
    )

    assert spec.model == "deepseek/deepseek-flash"


def test_non_replay_launch_keeps_model() -> None:
    assert _resolve().model == "deepseek/deepseek-flash"
    assert (
        _resolve(continue_harness_session_id="ses-parent", continue_fork=True).model
        == "deepseek/deepseek-flash"
    )
