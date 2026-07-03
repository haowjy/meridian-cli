"""SpawnContinueInput flow — resume and fork continuation from a source spawn.

SpawnForkInput tests live in test_spawn_fork.py and test_spawn_fork_harness.py.

# qa-validated: test-suite-redesign
"""

from pathlib import Path
from typing import Any, cast

import pytest

import meridian.lib.ops.spawn.api as spawn_api
from meridian.lib.core.domain import SkillContent
from meridian.lib.core.execution_policy import ResolvedExecutionPolicy
from meridian.lib.core.launch_policy_snapshot import LaunchPolicySnapshot
from meridian.lib.core.types import HarnessId
from meridian.lib.harness.registry import get_default_harness_registry
from meridian.lib.launch.composition_spawn import bind_spawn_launch_context
from meridian.lib.launch.context import RuntimeBindings
from meridian.lib.launch.plan import build_spawn_mars_runtime
from meridian.lib.launch.request import LaunchArgvIntent, SpawnRequest
from meridian.lib.ops.reference import ResolvedSessionReference
from meridian.lib.ops.reference_recovery import RecoveryProvenance, RecoveryResult
from meridian.lib.ops.runtime import build_runtime
from meridian.lib.ops.spawn.execute_init import resolve_spawn_work_id
from meridian.lib.ops.spawn.models import SpawnActionOutput, SpawnContinueInput, SpawnCreateInput
from meridian.lib.ops.spawn.prepare import build_create_payload
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
    work_id: str | None = "w-spawn",
    task_cwd: str | None = None,
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
        work_id=work_id,
        harness_session_id=harness_session_id,
        task_cwd=task_cwd,
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

    def resolve_claude_harness(
        _create_input: SpawnCreateInput,
        *,
        resolved_project_root: Path | None = None,
    ) -> str:
        _ = resolved_project_root
        return "claude"

    monkeypatch.setattr(
        spawn_api,
        "_resolve_effective_fork_target_harness",
        resolve_claude_harness,
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
    ("updates", "flag", "guidance"),
    [
        ({"agent": "reviewer"}, "--agent", "--fork-fresh"),
        ({"skills": ("skill-override",)}, "--skills", "--fork-fresh"),
        ({"approval": "auto"}, "--approval", "--fork-fresh"),
        ({"sandbox": "workspace-write"}, "--sandbox", "--fork-fresh"),
        ({"effort": "high"}, "--effort", "--fork-fresh"),
        ({"autocompact": 5000}, "--autocompact", "--fork-fresh"),
        ({"autocompact_pct": 35}, "--autocompact-pct", "--fork-fresh"),
        ({"passthrough_args": ("--custom",)}, "--", "--fork-fresh"),
        ({"work": "other-work"}, "--work", "--fork-fresh"),
        ({"task_dir": "../other"}, "--task-dir", "--fork --task-dir"),
    ],
)
def test_spawn_continue_rejects_policy_flags_without_snapshot(
    tmp_path: Path,
    updates: dict[str, object],
    flag: str,
    guidance: str,
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
                **cast("Any", updates),
            )
        )

    message = str(exc_info.value)
    assert flag in message
    assert guidance in message


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
    source_task_dir = tmp_path / "source-worktree"
    source_task_dir.mkdir()
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
        task_cwd=source_task_dir.as_posix(),
        launch_policy_snapshot=snapshot,
    )
    monkeypatch.setenv("MERIDIAN_MODEL", "gpt-5.4")
    monkeypatch.setenv("MERIDIAN_APPROVAL", "confirm")
    monkeypatch.setenv("MERIDIAN_SANDBOX", "read-only")
    monkeypatch.setenv("MERIDIAN_AGENT", "other-agent")

    def fail_bundle_resolution(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("snapshot replay should not re-resolve live policy")

    monkeypatch.setattr(
        "meridian.lib.launch.bundle_adapter.request_and_resolve",
        fail_bundle_resolution,
    )

    captured_payload: list[SpawnCreateInput] = []
    captured_request: list[SpawnRequest] = []

    def fake_execute_spawn_blocking(
        *,
        payload: SpawnCreateInput,
        request: SpawnRequest,
        runtime: object,
        ctx: object = None,
        prepared: object = None,
        on_spawn_id: object = None,
    ) -> SpawnActionOutput:
        _ = (runtime, ctx, prepared, on_spawn_id)
        captured_payload.append(payload)
        captured_request.append(request)
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
    spawn_request = captured_request[0]
    continue_input = captured_payload[0]
    assert continue_input.work == "w-spawn"
    assert continue_input.task_dir == source_task_dir.as_posix()
    assert continue_input.launch_policy_snapshot == snapshot
    assert spawn_request.launch_policy_snapshot == snapshot
    assert spawn_request.launch_policy_snapshot is not None
    assert spawn_request.work_id_hint == "w-spawn"
    assert spawn_request.task_cwd == source_task_dir.as_posix()
    assert spawn_request.task_cwd_work_item == "w-spawn"
    assert continue_input.model == snapshot.model

    artifacts = build_create_payload(continue_input, runtime=build_runtime(project_root))
    resolved = artifacts.prepared.request
    assert resolved.model == snapshot.model
    assert resolved.harness == snapshot.harness
    assert resolved.agent == snapshot.agent
    assert resolved.skills == snapshot.skills
    assert resolved.execution_policy == snapshot.execution_policy
    assert resolved.tools == snapshot.tools
    assert resolved.mcp_tools == snapshot.mcp_tools
    assert resolved.extra_args == snapshot.extra_args


def test_spawn_continue_replays_snapshot_harness_when_reference_harness_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = _state_root(project_root)
    snapshot = LaunchPolicySnapshot(model="gpt-5.3-codex", harness="codex", agent="coder")
    _seed_spawn(
        runtime_root,
        spawn_id="p46",
        harness_session_id="session-46",
        launch_policy_snapshot=snapshot,
    )

    def resolve_without_harness(
        project_root: Path,
        ref: str,
        *,
        runtime_root: Path | None = None,
    ) -> ResolvedSessionReference:
        _ = (project_root, ref, runtime_root)
        return ResolvedSessionReference(
            harness_session_id="session-46",
            harness=None,
            source_chat_id="c-seed",
            source_model=snapshot.model,
            source_agent=snapshot.agent,
            source_skills=snapshot.skills,
            source_work_id="w-spawn",
            tracked=True,
            source_launch_policy_snapshot=snapshot,
        )

    monkeypatch.setattr(spawn_api, "resolve_session_reference", resolve_without_harness)

    captured_request: list[SpawnRequest] = []

    def fake_execute_spawn_blocking(
        *,
        payload: SpawnCreateInput,
        request: SpawnRequest,
        runtime: object,
        ctx: object = None,
        prepared: object = None,
        on_spawn_id: object = None,
    ) -> SpawnActionOutput:
        _ = (payload, runtime, ctx, prepared, on_spawn_id)
        captured_request.append(request)
        return SpawnActionOutput(command="spawn.create", status="running", spawn_id="p46c")

    monkeypatch.setattr(spawn_api, "execute_spawn_blocking", fake_execute_spawn_blocking)

    result = spawn_api.spawn_continue_sync(
        SpawnContinueInput(
            spawn_id="p46",
            prompt="follow-up prompt",
            project_root=project_root.as_posix(),
        )
    )

    assert result.command == "spawn.continue"
    assert captured_request[0].harness == "codex"
    assert captured_request[0].launch_policy_snapshot == snapshot


def test_spawn_continue_replays_codex_empty_model_snapshot_as_default_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = _state_root(project_root)
    snapshot = LaunchPolicySnapshot(
        model="",
        harness="codex",
        agent="coder",
        skills=(),
        execution_policy=ResolvedExecutionPolicy(approval="default", sandbox="workspace-write"),
    )
    _seed_spawn(
        runtime_root,
        spawn_id="p45",
        harness_session_id="session-45",
        launch_policy_snapshot=snapshot,
    )
    monkeypatch.setenv("MERIDIAN_MODEL", "claude-sonnet-4-6")

    def fail_bundle_resolution(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("empty-model snapshot replay should not call mars launch-bundle")

    monkeypatch.setattr(
        "meridian.lib.launch.bundle_adapter.request_and_resolve",
        fail_bundle_resolution,
    )

    captured_payload: list[SpawnCreateInput] = []

    def fake_execute_spawn_blocking(
        *,
        payload: SpawnCreateInput,
        request: SpawnRequest,
        runtime: object,
        ctx: object = None,
        prepared: object = None,
        on_spawn_id: object = None,
    ) -> SpawnActionOutput:
        _ = (request, runtime, ctx, prepared, on_spawn_id)
        captured_payload.append(payload)
        return SpawnActionOutput(command="spawn.create", status="running", spawn_id="p45c")

    monkeypatch.setattr(spawn_api, "execute_spawn_blocking", fake_execute_spawn_blocking)

    spawn_api.spawn_continue_sync(
        SpawnContinueInput(
            spawn_id="p45",
            prompt="follow-up prompt",
            project_root=project_root.as_posix(),
        )
    )

    continue_input = captured_payload[0]
    artifacts = build_create_payload(continue_input, runtime=build_runtime(project_root))
    resolved = artifacts.prepared.request
    assert resolved.model is None
    assert resolved.harness == "codex"
    launch_runtime = build_spawn_mars_runtime(
        runtime=build_runtime(project_root),
        runtime_root=runtime_root,
        control_root=project_root,
        execution_cwd=project_root.as_posix(),
        argv_intent=LaunchArgvIntent.REQUIRED,
    )
    ctx = bind_spawn_launch_context(
        prepared=artifacts.prepared,
        bindings=RuntimeBindings(spawn_id="p45-replay", dry_run=True),
        runtime=launch_runtime,
        harness_registry=get_default_harness_registry(),
    )
    assert ctx.model_selection is None
    assert ctx.binding.run_params.model is None
    assert ctx.binding.spec.model is None
    assert "--model" not in ctx.binding.argv

def test_spawn_continue_from_source_without_work_suppresses_ambient_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    source_task_dir = tmp_path / "source-worktree"
    source_task_dir.mkdir()
    ambient_work_dir = tmp_path / "ambient-work"
    ambient_work_dir.mkdir()
    runtime_root = _state_root(project_root)
    _seed_spawn(
        runtime_root,
        spawn_id="p29",
        harness_session_id="session-29",
        work_id=None,
        task_cwd=source_task_dir.as_posix(),
        launch_policy_snapshot=LaunchPolicySnapshot(model="gpt-5.3-codex", harness="codex"),
    )
    monkeypatch.setenv("MERIDIAN_ACTIVE_WORK_ID", "ambient-work")
    monkeypatch.setenv("MERIDIAN_ACTIVE_WORK_DIR", ambient_work_dir.as_posix())

    captured_payload: list[SpawnCreateInput] = []
    captured_request: list[SpawnRequest] = []

    def fake_execute_spawn_blocking(
        *,
        payload: SpawnCreateInput,
        request: SpawnRequest,
        runtime: object,
        ctx: object = None,
        prepared: object = None,
        on_spawn_id: object = None,
    ) -> SpawnActionOutput:
        _ = (runtime, ctx, prepared, on_spawn_id)
        captured_payload.append(payload)
        captured_request.append(request)
        return SpawnActionOutput(command="spawn.create", status="running", spawn_id="p29c")

    monkeypatch.setattr(spawn_api, "execute_spawn_blocking", fake_execute_spawn_blocking)

    result = spawn_api.spawn_continue_sync(
        SpawnContinueInput(
            spawn_id="p29",
            prompt="follow-up prompt",
            project_root=project_root.as_posix(),
        )
    )

    assert result.command == "spawn.continue"
    continue_input = captured_payload[0]
    create_request = captured_request[0]
    assert continue_input.work == ""
    assert continue_input.task_dir == source_task_dir.as_posix()
    assert create_request.work_id_hint is None
    assert create_request.task_cwd == source_task_dir.as_posix()
    assert create_request.task_cwd_work_item is None
    assert resolve_spawn_work_id(continue_input, create_request) is None


def test_spawn_continue_from_source_without_task_dir_suppresses_ambient_task_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    ambient_task_dir = tmp_path / "ambient-task"
    ambient_task_dir.mkdir()
    runtime_root = _state_root(project_root)
    _seed_spawn(
        runtime_root,
        spawn_id="p299",
        harness_session_id="session-299",
        work_id=None,
        task_cwd=None,
        launch_policy_snapshot=LaunchPolicySnapshot(model="gpt-5.3-codex", harness="codex"),
    )
    monkeypatch.setenv("MERIDIAN_TASK_DIR", ambient_task_dir.as_posix())

    captured_payload: list[SpawnCreateInput] = []
    captured_request: list[SpawnRequest] = []

    def fake_execute_spawn_blocking(
        *,
        payload: SpawnCreateInput,
        request: SpawnRequest,
        runtime: object,
        ctx: object = None,
        prepared: object = None,
        on_spawn_id: object = None,
    ) -> SpawnActionOutput:
        _ = (runtime, ctx, prepared, on_spawn_id)
        captured_payload.append(payload)
        captured_request.append(request)
        return SpawnActionOutput(command="spawn.create", status="running", spawn_id="p299c")

    monkeypatch.setattr(spawn_api, "execute_spawn_blocking", fake_execute_spawn_blocking)

    result = spawn_api.spawn_continue_sync(
        SpawnContinueInput(
            spawn_id="p299",
            prompt="follow-up prompt",
            project_root=project_root.as_posix(),
        )
    )

    assert result.command == "spawn.continue"
    assert captured_payload[0].task_dir != ambient_task_dir.as_posix()
    assert captured_request[0].task_cwd != ambient_task_dir.as_posix()


def test_spawn_continue_uses_legacy_source_launch_policy_without_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    source_task_dir = tmp_path / "legacy-worktree"
    source_task_dir.mkdir()
    runtime_root = _state_root(project_root)
    _seed_spawn(
        runtime_root,
        spawn_id="p30",
        harness_session_id="session-30",
        task_cwd=source_task_dir.as_posix(),
    )

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

    captured_payload: list[SpawnCreateInput] = []
    captured_request: list[SpawnRequest] = []

    def fake_execute_spawn_blocking(
        *,
        payload: SpawnCreateInput,
        request: SpawnRequest,
        runtime: object,
        ctx: object = None,
        prepared: object = None,
        on_spawn_id: object = None,
    ) -> SpawnActionOutput:
        _ = (runtime, ctx, prepared, on_spawn_id)
        captured_payload.append(payload)
        captured_request.append(request)
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
    create_input = captured_request[0]
    continue_input = captured_payload[0]
    assert continue_input.work == "w-spawn"
    assert continue_input.task_dir == source_task_dir.as_posix()
    assert continue_input.launch_policy_snapshot is None
    assert create_input.work_id_hint == "w-spawn"
    assert create_input.task_cwd == source_task_dir.as_posix()
    assert create_input.task_cwd_work_item == "w-spawn"
    assert create_input.harness == "codex"
    assert create_input.model == "gpt-5.3-codex"


def test_spawn_continue_uses_authoritative_recovered_session_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = _state_root(project_root)
    _seed_spawn(runtime_root, spawn_id="p32", harness_session_id=None)

    def resolve_recovered(
        project_root: Path,
        ref: str,
        *,
        runtime_root: Path | None = None,
    ) -> ResolvedSessionReference:
        _ = (project_root, ref, runtime_root)
        return ResolvedSessionReference(
            harness_session_id=None,
            harness="codex",
            source_chat_id="c-seed",
            source_model="gpt-5.3-codex",
            source_agent=None,
            source_skills=("skill-c",),
            source_work_id="w-spawn",
            tracked=True,
            source_execution_cwd=tmp_path.as_posix(),
            recovery=RecoveryResult(
                harness_session_id="recovered-session",
                provenance=RecoveryProvenance.SESSION_STORE,
            ),
        )

    monkeypatch.setattr(spawn_api, "resolve_session_reference", resolve_recovered)
    stub_bundle_request_and_resolve(
        monkeypatch,
        model="gpt-5.3-codex",
        model_token="gpt-5.3-codex",
        harness=HarnessId.CODEX,
        harness_model="openai/gpt-5.3-codex",
    )

    captured_payload: list[SpawnCreateInput] = []

    def fake_execute_spawn_blocking(
        *,
        payload: SpawnCreateInput,
        request: SpawnRequest,
        runtime: object,
        ctx: object = None,
        prepared: object = None,
        on_spawn_id: object = None,
    ) -> SpawnActionOutput:
        _ = (request, runtime, ctx, prepared, on_spawn_id)
        captured_payload.append(payload)
        return SpawnActionOutput(command="spawn.create", status="running", spawn_id="p32c")

    monkeypatch.setattr(spawn_api, "execute_spawn_blocking", fake_execute_spawn_blocking)

    spawn_api.spawn_continue_sync(
        SpawnContinueInput(
            spawn_id="p32",
            prompt="follow-up prompt",
            project_root=project_root.as_posix(),
        )
    )

    assert captured_payload[0].session.requested_harness_session_id == "recovered-session"


def test_spawn_fork_uses_authoritative_recovered_session_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    _state_root(project_root)

    def resolve_recovered(
        project_root: Path,
        ref: str,
        *,
        runtime_root: Path | None = None,
    ) -> ResolvedSessionReference:
        _ = (project_root, ref, runtime_root)
        return ResolvedSessionReference(
            harness_session_id=None,
            harness="codex",
            source_chat_id="c-seed",
            source_model="gpt-5.3-codex",
            source_agent=None,
            source_skills=("skill-c",),
            source_work_id="w-spawn",
            tracked=True,
            source_execution_cwd=tmp_path.as_posix(),
            recovery=RecoveryResult(
                harness_session_id="recovered-session",
                provenance=RecoveryProvenance.SPAWN_ROW,
            ),
        )

    monkeypatch.setattr(spawn_api, "resolve_session_reference", resolve_recovered)
    stub_bundle_request_and_resolve(
        monkeypatch,
        model="gpt-5.3-codex",
        model_token="gpt-5.3-codex",
        harness=HarnessId.CODEX,
        harness_model="openai/gpt-5.3-codex",
    )

    captured_payload: list[SpawnCreateInput] = []

    def fake_execute_spawn_blocking(
        *,
        payload: SpawnCreateInput,
        request: SpawnRequest,
        runtime: object,
        ctx: object = None,
        prepared: object = None,
        on_spawn_id: object = None,
    ) -> SpawnActionOutput:
        _ = (request, runtime, ctx, prepared, on_spawn_id)
        captured_payload.append(payload)
        return SpawnActionOutput(command="spawn.create", status="running", spawn_id="p33c")

    monkeypatch.setattr(spawn_api, "execute_spawn_blocking", fake_execute_spawn_blocking)

    spawn_api.spawn_fork_sync(
        spawn_api.SpawnForkInput(
            source_ref="p33",
            prompt="fork prompt",
            project_root=project_root.as_posix(),
        )
    )

    assert captured_payload[0].session.requested_harness_session_id == "recovered-session"
