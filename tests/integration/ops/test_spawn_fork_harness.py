"""SpawnForkInput — cross-harness validation and prepared-context tests.

Policy/goal/model inheritance tests live in test_spawn_fork.py.

# qa-validated: test-suite-redesign
"""

from dataclasses import replace
from pathlib import Path

import pytest

import meridian.lib.ops.spawn.api as spawn_api
from meridian.lib.bootstrap.services import prepare_for_runtime_write
from meridian.lib.core.types import HarnessId
from meridian.lib.launch import bundle_adapter
from meridian.lib.launch.launch_types import ResolvedExecutionPolicy
from meridian.lib.ops.reference import ResolvedSessionReference
from meridian.lib.ops.spawn.models import (
    SpawnActionOutput,
    SpawnCreateInput,
    SpawnForkInput,
)
from meridian.lib.state.paths import resolve_project_runtime_root


class _FakeBundleResult:
    def __init__(
        self,
        *,
        model: str,
        model_token: str,
        harness: HarnessId,
        harness_model: str | None,
        execution_policy: ResolvedExecutionPolicy,
        provenance: dict[str, str],
        warnings: tuple[str, ...] = (),
        tools_allowed: tuple[str, ...] = (),
        tools_disallowed: tuple[str, ...] = (),
        tools_mcp: tuple[str, ...] = (),
    ) -> None:
        self.model = model
        self.model_token = model_token
        self.harness = harness
        self.harness_model = harness_model
        self.execution_policy = execution_policy
        self.provenance = provenance
        self.warnings = warnings
        self.tools_allowed = tools_allowed
        self.tools_disallowed = tools_disallowed
        self.tools_mcp = tools_mcp


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


def _fake_codex_session_reference(*_args, **_kwargs):
    return _resolved_reference()


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


def test_spawn_fork_rejects_cross_harness_when_model_infers_different_target(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    _state_root(project_root)
    monkeypatch.setattr(spawn_api, "resolve_session_reference", _fake_codex_session_reference)

    captured_request = None

    def _fake_request_and_resolve(request, *, harness_registry):
        _ = harness_registry
        nonlocal captured_request
        captured_request = request
        return _FakeBundleResult(
            model=request.model_override or "gpt-5.4-mini",
            model_token=request.model_override or "gpt-5.4-mini",
            harness=HarnessId.CLAUDE if request.model_override == "haiku" else HarnessId.CODEX,
            harness_model=request.model_override or "gpt-5.4-mini",
            execution_policy=ResolvedExecutionPolicy(),
            provenance={"model_source": "cli", "harness_source": "provider"},
        )

    monkeypatch.setattr(bundle_adapter, "request_and_resolve", _fake_request_and_resolve)

    def _fail_spawn_create_sync(*_args, **_kwargs):
        raise AssertionError("cross-harness fork should fail before spawn_create_sync")

    monkeypatch.setattr(spawn_api, "spawn_create_sync", _fail_spawn_create_sync)

    with pytest.raises(ValueError, match="Cannot fork across harnesses"):
        spawn_api.spawn_fork_sync(
            SpawnForkInput(
                source_ref="c-source",
                prompt="fork prompt",
                model="haiku",
                project_root=project_root.as_posix(),
            )
        )
    assert captured_request is not None
    assert captured_request.model_override == "haiku"
    assert captured_request.harness_override is None


def test_spawn_fork_with_prepared_context_uses_prepared_root_for_harness_preview(
    tmp_path: Path,
    monkeypatch,
) -> None:
    ambient_root = tmp_path / "ambient"
    ambient_root.mkdir()
    monkeypatch.chdir(ambient_root)

    project_root = tmp_path / "repo"
    project_root.mkdir()
    _state_root(project_root)
    prepared = prepare_for_runtime_write(project_root)
    monkeypatch.setattr(spawn_api, "resolve_session_reference", _fake_codex_session_reference)

    captured_bundle_request = None

    def _fake_request_and_resolve(request, *, harness_registry):
        _ = harness_registry
        nonlocal captured_bundle_request
        captured_bundle_request = request
        expected_root = project_root.resolve()
        request_root = request.project_root.resolve()
        harness = HarnessId.CODEX if request_root == expected_root else HarnessId.CLAUDE
        return _FakeBundleResult(
            model=request.model_override or "gpt-5.4-mini",
            model_token=request.model_override or "gpt-5.4-mini",
            harness=harness,
            harness_model=request.model_override or "gpt-5.4-mini",
            execution_policy=ResolvedExecutionPolicy(),
            provenance={"model_source": "cli", "harness_source": "provider"},
        )

    monkeypatch.setattr(bundle_adapter, "request_and_resolve", _fake_request_and_resolve)

    captured_input: SpawnCreateInput | None = None

    def _fake_spawn_create_sync(
        payload: SpawnCreateInput,
        ctx=None,
        *,
        sink=None,
        prepared=None,
    ) -> SpawnActionOutput:
        _ = (ctx, sink, prepared)
        nonlocal captured_input
        captured_input = payload
        return SpawnActionOutput(command="spawn.create", status="dry-run")

    monkeypatch.setattr(spawn_api, "spawn_create_sync", _fake_spawn_create_sync)

    result = spawn_api.spawn_fork_sync(
        SpawnForkInput(source_ref="c-source", prompt="fork prompt", model="gpt-5.4-mini"),
        prepared=prepared,
    )

    assert result.status == "dry-run"
    assert captured_input is not None
    assert captured_input.harness == "codex"
    assert captured_bundle_request is not None
    assert captured_bundle_request.project_root.resolve() == project_root.resolve()


def test_spawn_fork_rejects_cross_harness_when_payload_harness_is_explicit(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    _state_root(project_root)
    monkeypatch.setattr(spawn_api, "resolve_session_reference", _fake_codex_session_reference)

    captured_request = None

    def _fake_request_and_resolve(request, *, harness_registry):
        _ = harness_registry
        nonlocal captured_request
        captured_request = request
        harness = HarnessId.CLAUDE if request.harness_override == "claude" else HarnessId.CODEX
        return _FakeBundleResult(
            model=request.model_override or "gpt-5.4-mini",
            model_token=request.model_override or "gpt-5.4-mini",
            harness=harness,
            harness_model=request.model_override or "gpt-5.4-mini",
            execution_policy=ResolvedExecutionPolicy(),
            provenance={"model_source": "cli", "harness_source": "cli"},
        )

    monkeypatch.setattr(bundle_adapter, "request_and_resolve", _fake_request_and_resolve)

    def _fail_spawn_create_sync(*_args, **_kwargs):
        raise AssertionError("cross-harness fork should fail before spawn_create_sync")

    monkeypatch.setattr(spawn_api, "spawn_create_sync", _fail_spawn_create_sync)

    with pytest.raises(ValueError, match="Cannot fork across harnesses"):
        spawn_api.spawn_fork_sync(
            SpawnForkInput(
                source_ref="c-source",
                prompt="fork prompt",
                model="gpt-5.4-mini",
                harness="claude",
                project_root=project_root.as_posix(),
            )
        )
    assert captured_request is not None
    assert captured_request.model_override == "gpt-5.4-mini"
    assert captured_request.harness_override == "claude"


def test_spawn_fork_errors_when_reference_has_no_recorded_session(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    _state_root(project_root)
    monkeypatch.setattr(
        spawn_api,
        "resolve_session_reference",
        lambda *_args, **_kwargs: _resolved_reference(harness_session_id=None),
    )

    with pytest.raises(ValueError) as exc_info:
        spawn_api.spawn_fork_sync(
            SpawnForkInput(
                source_ref="c7",
                prompt="fork prompt",
                project_root=project_root.as_posix(),
            )
        )

    assert (
        str(exc_info.value)
        == "Session 'c7' has no recorded harness session — cannot continue/fork."
    )
