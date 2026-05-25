"""SpawnContinueInput flow — resume and fork continuation from a source spawn.

SpawnForkInput tests live in test_spawn_fork.py and test_spawn_fork_harness.py.

# qa-validated: test-suite-redesign
"""

from pathlib import Path

import pytest

import meridian.lib.ops.spawn.api as spawn_api
from meridian.lib.core.domain import SkillContent
from meridian.lib.core.execution_policy import ResolvedExecutionPolicy
from meridian.lib.core.launch_policy_snapshot import LaunchPolicySnapshot
from meridian.lib.core.types import HarnessId
from meridian.lib.ops.spawn.models import SpawnActionOutput, SpawnContinueInput, SpawnCreateInput
from meridian.lib.state import spawn_store
from meridian.lib.state.paths import resolve_project_runtime_root
from tests.support.fixtures import write_skill
from tests.support.launch import stub_bundle_request_and_resolve


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


def _seed_spawn(
    runtime_root: Path,
    *,
    spawn_id: str,
    harness_session_id: str | None,
    prompt: str = "seed prompt",
    goal: str | None = None,
    execution_cwd: str | None = None,
    launch_policy_snapshot: LaunchPolicySnapshot | None = None,
) -> None:
    model = launch_policy_snapshot.model if launch_policy_snapshot is not None else "gpt-5.3-codex"
    agent = (
        (launch_policy_snapshot.agent or "coder")
        if launch_policy_snapshot is not None
        else "coder"
    )
    skills = launch_policy_snapshot.skills if launch_policy_snapshot is not None else ("skill-c",)
    harness = launch_policy_snapshot.harness if launch_policy_snapshot is not None else "codex"
    spawn_store.start_spawn(
        runtime_root,
        spawn_id=spawn_id,
        chat_id="c-seed",
        model=model,
        agent=agent,
        skills=skills,
        harness=harness,
        prompt=prompt,
        goal=goal,
        work_id="w-spawn",
        harness_session_id=harness_session_id,
        execution_cwd=execution_cwd,
        launch_policy_snapshot=launch_policy_snapshot,
    )


def test_spawn_continue_errors_when_source_spawn_lacks_harness_session_id(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = _state_root(project_root)
    _seed_spawn(runtime_root, spawn_id="p11", harness_session_id=None)

    try:
        spawn_api.spawn_continue_sync(
            SpawnContinueInput(
                spawn_id="p11",
                prompt="follow-up prompt",
                project_root=project_root.as_posix(),
            )
        )
    except ValueError as exc:
        assert str(exc) == "Spawn 'p11' has no recorded session — cannot continue/fork."
    else:  # pragma: no cover - defensive assertion
        raise AssertionError("Expected continue from missing harness session to fail.")


def test_spawn_continue_errors_on_explicit_harness_conflict(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = _state_root(project_root)
    _seed_spawn(runtime_root, spawn_id="p24", harness_session_id="session-24")

    with pytest.raises(ValueError) as exc_info:
        spawn_api.spawn_continue_sync(
            SpawnContinueInput(
                spawn_id="p24",
                prompt="follow-up prompt",
                harness="claude",
                project_root=project_root.as_posix(),
            )
        )

    assert "--harness" in str(exc_info.value)
    assert "--fork-fresh" in str(exc_info.value)


def test_spawn_continue_rejects_model_override_before_legacy_fallback_resolution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = _state_root(project_root)
    _seed_spawn(runtime_root, spawn_id="p26", harness_session_id="session-26")
    monkeypatch.setattr(
        spawn_api,
        "_resolve_effective_fork_target_harness",
        lambda _create_input, *, resolved_project_root=None: "claude",
    )

    with pytest.raises(ValueError) as exc_info:
        spawn_api.spawn_continue_sync(
            SpawnContinueInput(
                spawn_id="p26",
                prompt="follow-up prompt",
                model="claude-sonnet-4.5",
                project_root=project_root.as_posix(),
            )
        )

    assert "--model" in str(exc_info.value)
    assert "--fork-fresh" in str(exc_info.value)


@pytest.mark.parametrize(
    ("updates", "flag"),
    [
        ({"agent": "reviewer"}, "--agent"),
        ({"skills": ("skill-override",)}, "--skills"),
        ({"approval": "auto"}, "--approval"),
        ({"sandbox": "workspace-write"}, "--sandbox"),
        ({"effort": "high"}, "--effort"),
        ({"autocompact": 5000}, "--autocompact"),
        ({"autocompact_pct": 35}, "--autocompact-pct"),
        ({"passthrough_args": ("--custom",)}, "--"),
        ({"work": "other-work"}, "--work"),
        ({"worktree": True}, "--worktree/--no-worktree"),
        ({"repo": "../other"}, "--repo"),
    ],
)
def test_spawn_continue_rejects_policy_flags_without_snapshot(
    tmp_path: Path,
    updates: dict[str, object],
    flag: str,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = _state_root(project_root)
    _seed_spawn(runtime_root, spawn_id="p25", harness_session_id="session-25")

    with pytest.raises(ValueError) as exc_info:
        spawn_api.spawn_continue_sync(
            SpawnContinueInput(
                spawn_id="p25",
                prompt="follow-up prompt",
                project_root=project_root.as_posix(),
                **updates,
            )
        )

    message = str(exc_info.value)
    assert flag in message
    assert "--fork-fresh" in message


def test_spawn_continue_rejects_policy_flags_when_snapshot_exists(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = _state_root(project_root)
    _seed_spawn(
        runtime_root,
        spawn_id="p27",
        harness_session_id="session-27",
        launch_policy_snapshot=LaunchPolicySnapshot(model="gpt-5.3-codex", harness="codex"),
    )

    with pytest.raises(ValueError) as exc_info:
        spawn_api.spawn_continue_sync(
            SpawnContinueInput(
                spawn_id="p27",
                prompt="follow-up prompt",
                model="claude-sonnet-4-6",
                project_root=project_root.as_posix(),
            )
        )

    assert "--model" in str(exc_info.value)
    assert "--fork-fresh" in str(exc_info.value)


def test_spawn_continue_replays_source_launch_policy_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = _state_root(project_root)
    write_skill(project_root, "testing-principles")
    snapshot = LaunchPolicySnapshot(
        model="claude-sonnet-4-6",
        harness="claude",
        agent="coder",
        skills=("testing-principles",),
        loaded_skills=(
            SkillContent(
                name="testing-principles",
                description="testing-principles skill",
                path="/skills/testing-principles/SKILL.md",
                content="# testing-principles\n\nBe consistent.\n",
                skill_type="reference",
            ),
        ),
        execution_policy=ResolvedExecutionPolicy(approval="auto", sandbox="workspace-write"),
        tools={"write": "allow"},
        mcp_tools=("github",),
        extra_args=("--permission-mode", "acceptEdits"),
    )
    _seed_spawn(
        runtime_root,
        spawn_id="p28",
        harness_session_id="session-28",
        launch_policy_snapshot=snapshot,
    )
    monkeypatch.setenv("MERIDIAN_MODEL", "gpt-5.4")
    monkeypatch.setenv("MERIDIAN_APPROVAL", "confirm")
    monkeypatch.setenv("MERIDIAN_SANDBOX", "read-only")

    def fail_bundle_resolution(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("snapshot replay should not re-resolve live policy")

    monkeypatch.setattr(
        "meridian.lib.launch.bundle_adapter.request_and_resolve",
        fail_bundle_resolution,
    )

    captured: dict[str, object] = {}

    def fake_execute_spawn_blocking(
        *,
        payload: SpawnCreateInput,
        request: object,
        runtime: object,
        ctx: object = None,
    ) -> SpawnActionOutput:
        _ = (runtime, ctx)
        captured["payload"] = payload
        captured["request"] = request
        return SpawnActionOutput(command="spawn.create", status="running", spawn_id="p29")

    monkeypatch.setattr(spawn_api, "execute_spawn_blocking", fake_execute_spawn_blocking)

    result = spawn_api.spawn_continue_sync(
        SpawnContinueInput(
            spawn_id="p28",
            prompt="follow-up prompt",
            project_root=project_root.as_posix(),
        )
    )

    assert result.command == "spawn.continue"
    create_input = captured["request"]
    continue_input = captured["payload"]
    assert isinstance(continue_input, SpawnCreateInput)
    assert continue_input.launch_policy_snapshot == snapshot
    assert create_input.launch_policy_snapshot == snapshot
    assert create_input.model == snapshot.model
    assert create_input.harness == snapshot.harness
    assert create_input.agent == snapshot.agent
    assert create_input.skills == snapshot.skills
    assert create_input.launch_policy_snapshot.loaded_skills == snapshot.loaded_skills
    assert create_input.execution_policy.approval == snapshot.execution_policy.approval
    assert create_input.execution_policy.sandbox == snapshot.execution_policy.sandbox
    assert create_input.execution_policy.effort == snapshot.execution_policy.effort
    assert create_input.extra_args == snapshot.extra_args


def test_spawn_continue_uses_legacy_source_launch_policy_without_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = _state_root(project_root)
    _seed_spawn(runtime_root, spawn_id="p30", harness_session_id="session-30")

    monkeypatch.setenv("MERIDIAN_MODEL", "claude-sonnet-4-6")
    monkeypatch.setenv("MERIDIAN_APPROVAL", "confirm")
    stub_bundle_request_and_resolve(
        monkeypatch,
        model="gpt-5.3-codex",
        model_token="gpt-5.3-codex",
        harness=HarnessId.CODEX,
        harness_model="openai/gpt-5.3-codex",
    )

    def fail_live_harness_resolution(
        _create_input: SpawnCreateInput,
        *,
        resolved_project_root: Path | None = None,
    ) -> str:
        _ = resolved_project_root
        raise AssertionError("legacy continue should use the persisted source harness")

    monkeypatch.setattr(
        spawn_api,
        "_resolve_effective_fork_target_harness",
        fail_live_harness_resolution,
    )

    captured: dict[str, object] = {}

    def fake_execute_spawn_blocking(
        *,
        payload: SpawnCreateInput,
        request: object,
        runtime: object,
        ctx: object = None,
    ) -> SpawnActionOutput:
        _ = (runtime, ctx)
        captured["payload"] = payload
        captured["request"] = request
        return SpawnActionOutput(command="spawn.create", status="running", spawn_id="p31")

    monkeypatch.setattr(spawn_api, "execute_spawn_blocking", fake_execute_spawn_blocking)

    result = spawn_api.spawn_continue_sync(
        SpawnContinueInput(
            spawn_id="p30",
            prompt="follow-up prompt",
            project_root=project_root.as_posix(),
        )
    )

    assert result.command == "spawn.continue"
    create_input = captured["request"]
    continue_input = captured["payload"]
    assert isinstance(continue_input, SpawnCreateInput)
    assert continue_input.launch_policy_snapshot is None
    assert create_input.harness == "codex"
    assert create_input.model == "gpt-5.3-codex"
