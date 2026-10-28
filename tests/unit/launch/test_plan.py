"""Primary spawn-request construction: synthetic-prompt and context decisions."""

from __future__ import annotations

import pytest

from meridian.lib.core.context_ref import SpawnContextRef
from meridian.lib.core.types import HarnessId
from meridian.lib.launch.plan import build_primary_spawn_request
from meridian.lib.launch.types import LaunchRequest, build_primary_prompt


@pytest.mark.parametrize(
    ("harness", "requires_initial_prompt"),
    [
        (HarnessId.CLAUDE, False),
        (HarnessId.CODEX, True),
    ],
    ids=["claude", "codex"],
)
def test_build_primary_spawn_request_only_uses_synthetic_prompt_when_required(
    harness: HarnessId,
    requires_initial_prompt: bool,
) -> None:
    request = LaunchRequest(
        model="test-model",
        harness=harness.value,
    )

    spawn_request = build_primary_spawn_request(request=request)

    if requires_initial_prompt:
        assert spawn_request.prompt == build_primary_prompt(request)
        assert spawn_request.primary_prompt_is_synthetic is True
    else:
        assert spawn_request.prompt == ""
        assert spawn_request.primary_prompt_is_synthetic is False


def _prior(spawn_id: str = "p999", work_id: str | None = None) -> SpawnContextRef:
    """A resolved ``--from`` ref, as the cli or spawn prepare hands it to launch."""
    return SpawnContextRef(
        spawn_id=spawn_id,
        status="succeeded",
        agent="coder",
        desc="prior task",
        model="gpt-5.4-mini",
        harness="codex",
        report_text="prior context",
        work_id=work_id,
    )


def test_build_primary_spawn_request_uses_context_as_initial_prompt() -> None:
    request = LaunchRequest(
        model="test-model",
        harness=HarnessId.CODEX.value,
        context_refs=(_prior(),),
    )

    spawn_request = build_primary_spawn_request(request=request)

    assert spawn_request.prompt == ""
    assert spawn_request.primary_prompt_is_synthetic is False


@pytest.mark.parametrize(
    "model",
    ["claude-sonnet-4"],
    ids=["claude-model"],
)
def test_build_primary_spawn_request_without_harness_skips_synthetic_prompt(model: str) -> None:
    """Without explicit harness we default to no synthetic first prompt."""

    request = LaunchRequest(model=model)

    spawn_request = build_primary_spawn_request(request=request)

    assert spawn_request.prompt == ""
