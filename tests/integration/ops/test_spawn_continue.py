"""Spawn continue/fork adapter coverage.

Shared exact-continue semantics belong to ``test_continue_replay.py``. These
integration tests cover only spawn-surface validation, source resolution, and
the handoff to spawn creation.
"""

from pathlib import Path
from typing import Any, cast

import pytest

import meridian.lib.ops.spawn.api as spawn_api
from meridian.lib.core.launch_policy_snapshot import LaunchPolicySnapshot
from meridian.lib.core.types import HarnessId
from meridian.lib.launch.request import SpawnRequest
from meridian.lib.ops.reference import ResolvedSessionReference
from meridian.lib.ops.reference_recovery import RecoveryProvenance, RecoveryResult
from meridian.lib.ops.spawn.execute_init import resolve_spawn_work_id
from meridian.lib.ops.spawn.models import SpawnActionOutput, SpawnContinueInput, SpawnCreateInput
from meridian.lib.state import spawn_store
from meridian.lib.state.paths import resolve_project_runtime_root_for_write
from tests.support.launch import stub_bundle_request_and_resolve


def _state_root(project_root: Path) -> Path:
    (project_root / "mars.toml").write_text(
        '[settings]\ntargets = [".claude", ".codex", ".opencode"]\n',
        encoding="utf-8",
    )
    (project_root / "meridian.toml").write_text(
        "[spawn]\ndeny_headless_harnesses = []\n",
        encoding="utf-8",
    )
    runtime_root = resolve_project_runtime_root_for_write(project_root)
    runtime_root.mkdir(parents=True, exist_ok=True)
    return runtime_root


def _seed_spawn(
    runtime_root: Path,
    *,
    spawn_id: str,
    harness_session_id: str | None,
    work_id: str | None = "w-spawn",
    task_cwd: str | None = None,
    launch_policy_snapshot: LaunchPolicySnapshot | None = None,
) -> None:
    snapshot = launch_policy_snapshot
    spawn_store.start_spawn(
        runtime_root,
        spawn_id=spawn_id,
        chat_id="c-seed",
        model=snapshot.model if snapshot is not None else "gpt-5.3-codex",
        agent=(snapshot.agent or "coder") if snapshot is not None else "coder",
        skills=snapshot.skills if snapshot is not None else ("skill-c",),
        harness=snapshot.harness if snapshot is not None else "codex",
        prompt="seed prompt",
        work_id=work_id,
        harness_session_id=harness_session_id,
        task_cwd=task_cwd,
        launch_policy_snapshot=snapshot,
    )


def _record_spawn_create(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[SpawnCreateInput, SpawnRequest]]:
    calls: list[tuple[SpawnCreateInput, SpawnRequest]] = []

    def execute_spawn_blocking(
        *,
        payload: SpawnCreateInput,
        request: SpawnRequest,
        runtime: object,
        ctx: object = None,
        prepared: object = None,
        on_spawn_id: object = None,
    ) -> SpawnActionOutput:
        _ = (runtime, ctx, prepared, on_spawn_id)
        calls.append((payload, request))
        return SpawnActionOutput(
            command="spawn.create",
            status="running",
            spawn_id=f"p-captured-{len(calls)}",
        )

    monkeypatch.setattr(spawn_api, "execute_spawn_blocking", execute_spawn_blocking)
    return calls


def test_spawn_continue_requires_recorded_session(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = _state_root(project_root)
    _seed_spawn(runtime_root, spawn_id="p11", harness_session_id=None)

    with pytest.raises(ValueError, match="no recorded session"):
        spawn_api.spawn_continue_sync(
            SpawnContinueInput(
                spawn_id="p11",
                prompt="follow-up prompt",
                project_root=project_root.as_posix(),
            )
        )


@pytest.mark.parametrize(
    ("updates", "flag", "guidance"),
    [
        ({"model": "claude-sonnet-4-6"}, "--model", "--fork-fresh"),
        ({"agent": "reviewer"}, "--agent", "--fork-fresh"),
        ({"skills": ("skill-override",)}, "--skills", "--fork-fresh"),
        ({"approval": "auto"}, "--approval", "--fork-fresh"),
        ({"sandbox": "workspace-write"}, "--sandbox", "--fork-fresh"),
        ({"effort": "high"}, "--effort", "--fork-fresh"),
        ({"autocompact": 5000}, "--autocompact", "--fork-fresh"),
        ({"autocompact_pct": 35}, "--autocompact-pct", "--fork-fresh"),
        ({"passthrough_args": ("--custom",)}, "--", "--fork-fresh"),
        ({"env": ("NAME=value",)}, "--env", "--fork-fresh"),
        ({"work": "other-work"}, "--work", "--fork-fresh"),
        ({"task_dir": "../other"}, "--task-dir", "--fork --task-dir"),
    ],
)
def test_spawn_continue_rejects_policy_changes(
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


def test_spawn_continue_maps_source_contract_to_spawn_create(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    source_task_dir = tmp_path / "source-worktree"
    source_task_dir.mkdir()
    runtime_root = _state_root(project_root)
    snapshot = LaunchPolicySnapshot(
        model="claude-sonnet-4-6",
        harness="claude",
        agent="coder",
        extra_args=("--permission-mode", "acceptEdits"),
    )
    _seed_spawn(
        runtime_root,
        spawn_id="p28",
        harness_session_id="session-28",
        task_cwd=source_task_dir.as_posix(),
        launch_policy_snapshot=snapshot,
    )
    calls = _record_spawn_create(monkeypatch)

    result = spawn_api.spawn_continue_sync(
        SpawnContinueInput(
            spawn_id="p28",
            prompt="follow-up prompt",
            project_root=project_root.as_posix(),
        )
    )

    assert result.command == "spawn.continue"
    payload, request = calls[0]
    assert payload.launch_policy_snapshot == snapshot
    assert payload.work == "w-spawn"
    assert payload.task_dir == source_task_dir.as_posix()
    assert payload.model == snapshot.model
    assert payload.agent == snapshot.agent
    assert payload.passthrough_args == snapshot.extra_args
    assert request.launch_policy_snapshot == snapshot
    assert request.work_id_hint == "w-spawn"
    assert request.task_cwd == source_task_dir.as_posix()
    assert request.session.requested_harness_session_id == "session-28"


def test_spawn_continue_does_not_inherit_ambient_work_or_task_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    ambient_work_dir = tmp_path / "ambient-work"
    ambient_task_dir = tmp_path / "ambient-task"
    ambient_work_dir.mkdir()
    ambient_task_dir.mkdir()
    runtime_root = _state_root(project_root)
    _seed_spawn(
        runtime_root,
        spawn_id="p29",
        harness_session_id="session-29",
        work_id=None,
        task_cwd=None,
        launch_policy_snapshot=LaunchPolicySnapshot(model="gpt-5.3-codex", harness="codex"),
    )
    monkeypatch.setenv("MERIDIAN_ACTIVE_WORK_ID", "ambient-work")
    monkeypatch.setenv("MERIDIAN_ACTIVE_WORK_DIR", ambient_work_dir.as_posix())
    monkeypatch.setenv("MERIDIAN_TASK_DIR", ambient_task_dir.as_posix())
    calls = _record_spawn_create(monkeypatch)

    spawn_api.spawn_continue_sync(
        SpawnContinueInput(
            spawn_id="p29",
            prompt="follow-up prompt",
            project_root=project_root.as_posix(),
        )
    )

    payload, request = calls[0]
    assert payload.work == ""
    assert payload.task_dir == project_root.as_posix()
    assert request.work_id_hint is None
    assert request.task_cwd == project_root.as_posix()
    assert request.task_cwd_work_item is None
    assert resolve_spawn_work_id(payload, request) is None


def test_spawn_continue_legacy_source_uses_persisted_context(
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
    stub_bundle_request_and_resolve(
        monkeypatch,
        model="gpt-5.3-codex",
        model_token="gpt-5.3-codex",
        harness=HarnessId.CODEX,
        harness_model="openai/gpt-5.3-codex",
    )
    calls = _record_spawn_create(monkeypatch)

    spawn_api.spawn_continue_sync(
        SpawnContinueInput(
            spawn_id="p30",
            prompt="follow-up prompt",
            project_root=project_root.as_posix(),
        )
    )

    payload, request = calls[0]
    assert payload.launch_policy_snapshot is None
    assert payload.work == "w-spawn"
    assert payload.task_dir == source_task_dir.as_posix()
    assert request.work_id_hint == "w-spawn"
    assert request.task_cwd == source_task_dir.as_posix()
    assert request.harness == "codex"
    assert request.model == "gpt-5.3-codex"


def _recovered_reference(
    *,
    source_execution_cwd: str,
    provenance: RecoveryProvenance,
) -> ResolvedSessionReference:
    return ResolvedSessionReference(
        harness_session_id=None,
        harness="codex",
        source_chat_id="c-seed",
        source_model="gpt-5.3-codex",
        source_agent=None,
        source_skills=("skill-c",),
        source_work_id="w-spawn",
        tracked=True,
        source_execution_cwd=source_execution_cwd,
        recovery=RecoveryResult(
            harness_session_id="recovered-session",
            provenance=provenance,
        ),
    )


def test_spawn_continue_uses_recovered_session_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = _state_root(project_root)
    _seed_spawn(runtime_root, spawn_id="p32", harness_session_id=None)
    monkeypatch.setattr(
        spawn_api,
        "resolve_session_reference",
        lambda *_args, **_kwargs: _recovered_reference(
            source_execution_cwd=tmp_path.as_posix(),
            provenance=RecoveryProvenance.SESSION_STORE,
        ),
    )
    stub_bundle_request_and_resolve(
        monkeypatch,
        model="gpt-5.3-codex",
        model_token="gpt-5.3-codex",
        harness=HarnessId.CODEX,
        harness_model="openai/gpt-5.3-codex",
    )
    calls = _record_spawn_create(monkeypatch)

    spawn_api.spawn_continue_sync(
        SpawnContinueInput(
            spawn_id="p32",
            prompt="follow-up prompt",
            project_root=project_root.as_posix(),
        )
    )

    assert calls[0][0].session.requested_harness_session_id == "recovered-session"


def test_spawn_fork_uses_recovered_session_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    _state_root(project_root)
    monkeypatch.setattr(
        spawn_api,
        "resolve_session_reference",
        lambda *_args, **_kwargs: _recovered_reference(
            source_execution_cwd=tmp_path.as_posix(),
            provenance=RecoveryProvenance.SPAWN_ROW,
        ),
    )
    stub_bundle_request_and_resolve(
        monkeypatch,
        model="gpt-5.3-codex",
        model_token="gpt-5.3-codex",
        harness=HarnessId.CODEX,
        harness_model="openai/gpt-5.3-codex",
    )
    calls = _record_spawn_create(monkeypatch)

    spawn_api.spawn_fork_sync(
        spawn_api.SpawnForkInput(
            source_ref="p33",
            prompt="fork prompt",
            project_root=project_root.as_posix(),
        )
    )

    assert calls[0][0].session.requested_harness_session_id == "recovered-session"
