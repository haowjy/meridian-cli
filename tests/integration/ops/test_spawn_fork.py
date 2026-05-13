"""SpawnForkInput flow — policy/goal/model inheritance from source reference.

Cross-harness validation and prepared-context tests live in
test_spawn_fork_harness.py.

# qa-validated: test-suite-redesign
"""

from dataclasses import replace
from pathlib import Path

import meridian.lib.ops.spawn.api as spawn_api
from meridian.lib.ops.reference import ResolvedSessionReference
from meridian.lib.ops.spawn.models import (
    SpawnActionOutput,
    SpawnCreateInput,
    SpawnForkInput,
)
from meridian.lib.state import spawn_store
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


def _seed_spawn(
    runtime_root: Path,
    *,
    spawn_id: str,
    harness_session_id: str | None,
    goal: str | None = None,
) -> None:
    spawn_store.start_spawn(
        runtime_root,
        spawn_id=spawn_id,
        chat_id="c-seed",
        model="gpt-5.3-codex",
        agent="coder",
        harness="codex",
        prompt="seed prompt",
        goal=goal,
        harness_session_id=harness_session_id,
    )


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
        source_execution_cwd="/tmp/source-cwd",
        source_claude_config_dir=None,
        tracked=True,
    )
    if not overrides:
        return reference
    return replace(reference, **overrides)


def test_spawn_fork_inherits_policy_fields_from_resolved_reference(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    (project_root / "README.md").write_text("seed", encoding="utf-8")
    _state_root(project_root)
    monkeypatch.setattr(
        spawn_api,
        "_resolve_effective_fork_target_harness",
        lambda *_args, **_kwargs: "codex",
    )

    captured_input: SpawnCreateInput | None = None

    def _fake_resolve_session_reference(*_args, **_kwargs):
        return _resolved_reference(
            source_skills=("skill-a", "skill-b"),
            source_claude_config_dir="/tmp/source-claude",
        )

    def _fake_spawn_create_sync(
        payload: SpawnCreateInput,
        ctx=None,
        *,
        sink=None,
    ) -> SpawnActionOutput:
        _ = (ctx, sink)
        nonlocal captured_input
        captured_input = payload
        return SpawnActionOutput(command="spawn.create", status="dry-run")

    monkeypatch.setattr(spawn_api, "resolve_session_reference", _fake_resolve_session_reference)
    monkeypatch.setattr(spawn_api, "spawn_create_sync", _fake_spawn_create_sync)

    result = spawn_api.spawn_fork_sync(
        SpawnForkInput(
            source_ref="c-source",
            prompt="fork prompt",
            files=("README.md",),
            template_vars=("ticket=123",),
            project_root=project_root.as_posix(),
            inherit_source_skills=True,
        )
    )

    assert result.status == "dry-run"
    assert captured_input is not None
    assert captured_input.model == ""
    assert captured_input.agent is None
    assert captured_input.skills == ("skill-a", "skill-b")
    assert captured_input.files == ("README.md",)
    assert captured_input.template_vars == ("ticket=123",)
    assert captured_input.work == "w-source"
    assert captured_input.harness == "codex"
    assert captured_input.session.requested_harness_session_id == "session-seed"
    assert captured_input.session.continue_source_ref == "c-source"
    assert captured_input.session.continue_fork is True
    assert captured_input.session.forked_from_chat_id == "c-source"
    assert captured_input.session.source_control_root == "/tmp/source-root"
    assert captured_input.session.source_execution_cwd == "/tmp/source-cwd"
    assert captured_input.session.source_claude_config_dir == "/tmp/source-claude"


def test_spawn_fork_inherits_goal_for_concrete_spawn_ref_only(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = _state_root(project_root)
    _seed_spawn(runtime_root, spawn_id="p31", harness_session_id="session-31", goal="goal from p31")
    monkeypatch.setattr(
        spawn_api,
        "_resolve_effective_fork_target_harness",
        lambda *_args, **_kwargs: "codex",
    )

    captured_input: SpawnCreateInput | None = None

    monkeypatch.setattr(
        spawn_api,
        "resolve_session_reference",
        lambda *_args, **_kwargs: _resolved_reference(
            harness_session_id="session-31",
            source_model="gpt-5.4",
            source_agent="coder",
        ),
    )

    def _fake_spawn_create_sync(
        payload: SpawnCreateInput,
        ctx=None,
        *,
        sink=None,
    ) -> SpawnActionOutput:
        _ = (ctx, sink)
        nonlocal captured_input
        captured_input = payload
        return SpawnActionOutput(command="spawn.create", status="dry-run")

    monkeypatch.setattr(spawn_api, "spawn_create_sync", _fake_spawn_create_sync)

    result = spawn_api.spawn_fork_sync(
        SpawnForkInput(source_ref="p31", prompt="fork prompt", project_root=project_root.as_posix())
    )

    assert result.status == "dry-run"
    assert captured_input is not None
    assert captured_input.goal == "goal from p31"


def test_spawn_fork_does_not_inherit_goal_for_chat_ref(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    _state_root(project_root)
    monkeypatch.setattr(
        spawn_api,
        "_resolve_effective_fork_target_harness",
        lambda *_args, **_kwargs: "codex",
    )

    captured_input: SpawnCreateInput | None = None

    monkeypatch.setattr(
        spawn_api,
        "resolve_session_reference",
        lambda *_args, **_kwargs: _resolved_reference(
            harness_session_id="session-c7",
            source_model="gpt-5.4",
            source_agent="coder",
            source_chat_id="c7",
        ),
    )

    def _fake_spawn_create_sync(
        payload: SpawnCreateInput,
        ctx=None,
        *,
        sink=None,
    ) -> SpawnActionOutput:
        _ = (ctx, sink)
        nonlocal captured_input
        captured_input = payload
        return SpawnActionOutput(command="spawn.create", status="dry-run")

    monkeypatch.setattr(spawn_api, "spawn_create_sync", _fake_spawn_create_sync)

    result = spawn_api.spawn_fork_sync(
        SpawnForkInput(source_ref="c7", prompt="fork prompt", project_root=project_root.as_posix())
    )

    assert result.status == "dry-run"
    assert captured_input is not None
    assert captured_input.goal is None


def test_spawn_fork_goal_override_wins_over_inherited_goal(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = _state_root(project_root)
    _seed_spawn(runtime_root, spawn_id="p32", harness_session_id="session-32", goal="old fork goal")
    monkeypatch.setattr(
        spawn_api,
        "_resolve_effective_fork_target_harness",
        lambda *_args, **_kwargs: "codex",
    )

    captured_input: SpawnCreateInput | None = None

    monkeypatch.setattr(
        spawn_api,
        "resolve_session_reference",
        lambda *_args, **_kwargs: _resolved_reference(
            harness_session_id="session-32",
            source_model="gpt-5.4",
            source_agent="coder",
        ),
    )

    def _fake_spawn_create_sync(
        payload: SpawnCreateInput,
        ctx=None,
        *,
        sink=None,
    ) -> SpawnActionOutput:
        _ = (ctx, sink)
        nonlocal captured_input
        captured_input = payload
        return SpawnActionOutput(command="spawn.create", status="dry-run")

    monkeypatch.setattr(spawn_api, "spawn_create_sync", _fake_spawn_create_sync)

    result = spawn_api.spawn_fork_sync(
        SpawnForkInput(
            source_ref="p32",
            prompt="fork prompt",
            goal="  new fork goal  ",
            project_root=project_root.as_posix(),
        )
    )

    assert result.status == "dry-run"
    assert captured_input is not None
    assert captured_input.goal == "new fork goal"


def test_spawn_fork_uses_requested_model_agent_and_resolves_harness_from_policy(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    _state_root(project_root)
    monkeypatch.setattr(
        spawn_api,
        "_resolve_effective_fork_target_harness",
        lambda *_args, **_kwargs: "codex",
    )

    captured_input: SpawnCreateInput | None = None

    monkeypatch.setattr(
        spawn_api,
        "resolve_session_reference",
        lambda *_args, **_kwargs: _resolved_reference(
            source_model="gpt-5.4",
            source_skills=("skill-a",),
        ),
    )

    def _fake_spawn_create_sync(
        payload: SpawnCreateInput,
        ctx=None,
        *,
        sink=None,
    ) -> SpawnActionOutput:
        _ = (ctx, sink)
        nonlocal captured_input
        captured_input = payload
        return SpawnActionOutput(command="spawn.create", status="dry-run")

    monkeypatch.setattr(spawn_api, "spawn_create_sync", _fake_spawn_create_sync)

    result = spawn_api.spawn_fork_sync(
        SpawnForkInput(
            source_ref="c-source",
            prompt="fork prompt",
            project_root=project_root.as_posix(),
            model="gptmini",
            agent="architect",
            skills=("custom-skill",),
            inherit_source_skills=True,
        )
    )

    assert result.status == "dry-run"
    assert captured_input is not None
    assert captured_input.model == "gptmini"
    assert captured_input.agent == "architect"
    assert captured_input.skills == ("custom-skill",)
    assert captured_input.harness == "codex"
