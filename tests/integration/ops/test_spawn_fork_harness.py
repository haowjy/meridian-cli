"""SpawnForkInput — behavior-level cross-harness validation."""

from dataclasses import replace
from pathlib import Path

import pytest

import meridian.lib.ops.spawn.api as spawn_api
from meridian.lib.core.types import HarnessId
from meridian.lib.launch import bundle_adapter
from meridian.lib.launch.launch_types import ResolvedExecutionPolicy
from meridian.lib.ops.reference import ResolvedSessionReference
from meridian.lib.ops.spawn.models import SpawnForkInput
from meridian.lib.state.paths import resolve_project_runtime_root
from tests.support.launch import FakeBundleResult


def _state_root(project_root: Path) -> Path:
    mars_toml = project_root / "mars.toml"
    if not mars_toml.exists():
        mars_toml.write_text(
            '[settings]\ntargets = [".claude", ".codex", ".opencode"]\n',
            encoding="utf-8",
        )
    runtime_root = resolve_project_runtime_root(project_root)
    runtime_root.mkdir(parents=True, exist_ok=True)
    return runtime_root


def _resolved_reference(**overrides: object) -> ResolvedSessionReference:
    reference = ResolvedSessionReference(
        harness_session_id="session-seed",
        harness="codex",
        source_chat_id="c-source",
        source_model="",
        source_agent=None,
        source_skills=(),
        source_work_id="w-source",
        source_control_root="/tmp/source-root",
        source_execution_cwd=None,
        source_claude_config_dir=None,
        tracked=True,
    )
    if not overrides:
        return reference
    return replace(reference, **overrides)


def test_spawn_fork_rejects_cross_harness_when_target_model_resolves_elsewhere(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    _state_root(project_root)
    monkeypatch.setattr(
        spawn_api,
        "resolve_session_reference",
        lambda *_args, **_kwargs: _resolved_reference(),
    )

    def _fake_request_and_resolve(request, *, harness_registry):
        _ = harness_registry
        return FakeBundleResult(
            model=request.model_override or "gpt-5.4-mini",
            model_token=request.model_override or "gpt-5.4-mini",
            harness=HarnessId.CLAUDE if request.model_override == "haiku" else HarnessId.CODEX,
            harness_model=request.model_override or "gpt-5.4-mini",
            execution_policy=ResolvedExecutionPolicy(),
            provenance={"model_source": "cli", "harness_source": "provider"},
        )

    monkeypatch.setattr(bundle_adapter, "request_and_resolve", _fake_request_and_resolve)
    monkeypatch.setattr(
        spawn_api,
        "spawn_create_sync",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("cross-harness fork should fail before spawn creation")
        ),
    )

    with pytest.raises(ValueError, match="Cannot fork across harnesses"):
        spawn_api.spawn_fork_sync(
            SpawnForkInput(
                source_ref="c-source",
                prompt="fork prompt",
                model="haiku",
                project_root=project_root.as_posix(),
            )
        )
