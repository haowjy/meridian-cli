"""SpawnForkInput — cross-harness validation and prepared-context tests.

Policy/goal/model inheritance tests live in test_spawn_fork.py.

# qa-validated: test-suite-redesign
"""

from dataclasses import replace
from pathlib import Path

import pytest

import meridian.lib.ops.spawn.api as spawn_api
from meridian.lib.bootstrap.services import prepare_for_runtime_write
from meridian.lib.ops.reference import ResolvedSessionReference
from meridian.lib.ops.spawn.models import (
    SpawnActionOutput,
    SpawnCreateInput,
    SpawnForkInput,
)
from meridian.lib.state.paths import resolve_project_runtime_root


def _state_root(project_root: Path) -> Path:
    mars_toml = project_root / "mars.toml"
    if not mars_toml.exists():
        mars_toml.write_text(
            '[settings]\ntargets = [".claude"]\n',
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


def test_spawn_fork_rejects_cross_harness_when_env_selects_different_target(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("MERIDIAN_DEFAULT_HARNESS", "claude")
    project_root = tmp_path / "repo"
    project_root.mkdir()
    _state_root(project_root)
    monkeypatch.setattr(spawn_api, "resolve_session_reference", _fake_codex_session_reference)

    def _fail_spawn_create_sync(*_args, **_kwargs):
        raise AssertionError("cross-harness fork should fail before spawn_create_sync")

    monkeypatch.setattr(spawn_api, "spawn_create_sync", _fail_spawn_create_sync)

    with pytest.raises(ValueError, match="Cannot fork across harnesses"):
        spawn_api.spawn_fork_sync(
            SpawnForkInput(
                source_ref="c-source",
                prompt="fork prompt",
                project_root=project_root.as_posix(),
            )
        )


def test_spawn_fork_with_prepared_context_uses_prepared_root_for_harness_preview(
    tmp_path: Path,
    monkeypatch,
) -> None:
    ambient_root = tmp_path / "ambient"
    ambient_root.mkdir()
    (ambient_root / "meridian.toml").write_text(
        '[defaults]\nharness = "claude"\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(ambient_root)

    project_root = tmp_path / "repo"
    project_root.mkdir()
    _state_root(project_root)
    (project_root / "meridian.toml").write_text(
        '[defaults]\nharness = "codex"\n',
        encoding="utf-8",
    )
    prepared = prepare_for_runtime_write(project_root)
    monkeypatch.setattr(spawn_api, "resolve_session_reference", _fake_codex_session_reference)

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
        SpawnForkInput(source_ref="c-source", prompt="fork prompt"),
        prepared=prepared,
    )

    assert result.status == "dry-run"
    assert captured_input is not None
    assert captured_input.harness == "codex"


def test_spawn_fork_rejects_cross_harness_when_project_default_is_explicit(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    _state_root(project_root)
    (project_root / "meridian.toml").write_text(
        '[defaults]\nharness = "claude"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(spawn_api, "resolve_session_reference", _fake_codex_session_reference)

    def _fail_spawn_create_sync(*_args, **_kwargs):
        raise AssertionError("cross-harness fork should fail before spawn_create_sync")

    monkeypatch.setattr(spawn_api, "spawn_create_sync", _fail_spawn_create_sync)

    with pytest.raises(ValueError, match="Cannot fork across harnesses"):
        spawn_api.spawn_fork_sync(
            SpawnForkInput(
                source_ref="c-source",
                prompt="fork prompt",
                project_root=project_root.as_posix(),
            )
        )


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
