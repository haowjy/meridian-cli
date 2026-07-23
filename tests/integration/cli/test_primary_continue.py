"""Primary ``--continue`` adapter coverage.

Shared exact-continue semantics belong to ``test_continue_replay.py``. These
integration tests cover primary CLI validation, reference resolution, and the
handoff to the launch layer.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

import meridian.cli.primary_launch as primary_launch_module
import meridian.lib.launch.context as launch_context
from meridian.cli.primary_launch import PrimaryLaunchOutput, run_primary_launch
from meridian.lib.core.launch_policy_snapshot import LaunchPolicySnapshot
from meridian.lib.core.types import HarnessId
from meridian.lib.harness.registry import get_default_harness_registry
from meridian.lib.launch import LaunchRequest, LaunchResult, launch_primary
from meridian.lib.launch.process import ProcessOutcome
from meridian.lib.launch.request import SessionRequest
from meridian.lib.launch.types import SessionMode
from meridian.lib.state import session_store, spawn_store, work_repository, work_store
from meridian.lib.state.paths import resolve_project_paths, resolve_project_runtime_root_for_write
from tests.support.launch import stub_bundle_request_and_resolve


def _state_root(project_root: Path) -> Path:
    (project_root / "mars.toml").write_text(
        '[settings]\ntargets = [".claude", ".codex", ".opencode"]\n',
        encoding="utf-8",
    )
    runtime_root = resolve_project_runtime_root_for_write(project_root)
    runtime_root.mkdir(parents=True, exist_ok=True)
    return runtime_root


def _seed_primary_spawn(
    runtime_root: Path,
    *,
    spawn_id: str,
    harness_session_id: str,
    work_id: str | None = None,
    task_cwd: str | None = None,
    launch_policy_snapshot: LaunchPolicySnapshot | None = None,
) -> None:
    snapshot = launch_policy_snapshot
    spawn_store.start_spawn(
        runtime_root,
        spawn_id=spawn_id,
        chat_id="c-primary",
        model=snapshot.model if snapshot is not None else "gpt-5.3-codex",
        agent=(snapshot.agent or "agent-a") if snapshot is not None else "agent-a",
        skills=snapshot.skills if snapshot is not None else ("skill-a",),
        harness=snapshot.harness if snapshot is not None else "codex",
        kind="primary",
        prompt="primary prompt",
        work_id=work_id,
        task_cwd=task_cwd,
        harness_session_id=harness_session_id,
        launch_policy_snapshot=snapshot,
    )


def _run_primary_continue(
    project_root: Path,
    continue_ref: str,
    **overrides: Any,
) -> PrimaryLaunchOutput:
    arguments: dict[str, Any] = {
        "project_root": project_root,
        "continue_ref": continue_ref,
        "fork_ref": None,
        "fork_fresh_ref": None,
        "model": "",
        "harness": None,
        "agent": None,
        "work": "",
        "task_dir": None,
        "yolo": False,
        "approval": None,
        "autocompact": None,
        "effort": None,
        "sandbox": None,
        "timeout": None,
        "dry_run": False,
        "passthrough": (),
        "skills": (),
    }
    arguments.update(overrides)
    return run_primary_launch(**cast("Any", arguments))


def _record_primary_launch(monkeypatch: pytest.MonkeyPatch) -> list[LaunchRequest]:
    requests: list[LaunchRequest] = []

    def launch_primary(
        *,
        project_root: Path,
        request: LaunchRequest,
        harness_registry: object,
    ) -> LaunchResult:
        _ = (project_root, harness_registry)
        requests.append(request)
        return LaunchResult(
            command=(),
            exit_code=0,
            continue_ref=request.session.requested_harness_session_id,
            continue_chat_id=request.session.continue_chat_id,
        )

    monkeypatch.setattr(primary_launch_module, "launch_primary", launch_primary)
    return requests


def test_primary_continue_maps_source_contract_to_launch_request(
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
        agent="agent-a",
        skills=("testing-principles",),
        extra_args=("--permission-mode", "acceptEdits"),
    )
    _seed_primary_spawn(
        runtime_root,
        spawn_id="p41",
        harness_session_id="session-41",
        work_id="source-work",
        task_cwd=source_task_dir.as_posix(),
        launch_policy_snapshot=snapshot,
    )
    requests = _record_primary_launch(monkeypatch)

    _run_primary_continue(project_root, "p41")

    request = requests[0]
    assert request.launch_policy_snapshot == snapshot
    assert request.work_id == "source-work"
    assert request.task_dir == source_task_dir.as_posix()
    assert request.session.source_execution_cwd == source_task_dir.as_posix()
    assert request.session.requested_harness_session_id == "session-41"
    assert request.model == snapshot.model
    assert request.agent == snapshot.agent
    assert request.skills == snapshot.skills
    assert request.passthrough_args == snapshot.extra_args


def test_primary_continue_spawn_session_ref_uses_linked_spawn_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = _state_root(project_root)
    snapshot = LaunchPolicySnapshot(
        model="claude-sonnet-4-6",
        harness="claude",
        agent="agent-spawn",
        extra_args=("--permission-mode", "acceptEdits"),
    )
    spawn_store.start_spawn(
        runtime_root,
        spawn_id="p51",
        chat_id="c-spawn",
        owner_chat_id="c-primary",
        model=snapshot.model,
        agent=snapshot.agent or "agent-spawn",
        skills=snapshot.skills,
        harness=snapshot.harness,
        kind="child",
        prompt="child prompt",
        harness_session_id="session-spawn",
        launch_policy_snapshot=snapshot,
    )
    spawn_chat_id = session_store.start_session(
        runtime_root,
        harness="claude",
        harness_session_id="session-spawn",
        model=snapshot.model,
        chat_id="c-spawn",
        agent=snapshot.agent or "agent-spawn",
        skills=snapshot.skills,
        kind="spawn",
        spawn_id="p51",
    )
    requests = _record_primary_launch(monkeypatch)

    try:
        _run_primary_continue(project_root, spawn_chat_id)
    finally:
        session_store.stop_session(runtime_root, spawn_chat_id)

    assert requests[0].launch_policy_snapshot == snapshot
    assert requests[0].passthrough_args == snapshot.extra_args


def test_primary_continue_does_not_inherit_ambient_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    source_task_dir = tmp_path / "source-worktree"
    ambient_work_dir = tmp_path / "ambient-work"
    source_task_dir.mkdir()
    ambient_work_dir.mkdir()
    runtime_root = _state_root(project_root)
    _seed_primary_spawn(
        runtime_root,
        spawn_id="p45",
        harness_session_id="session-45",
        work_id=None,
        task_cwd=source_task_dir.as_posix(),
        launch_policy_snapshot=LaunchPolicySnapshot(model="gpt-5.3-codex", harness="codex"),
    )
    monkeypatch.setenv("MERIDIAN_ACTIVE_WORK_ID", "ambient-work")
    monkeypatch.setenv("MERIDIAN_ACTIVE_WORK_DIR", ambient_work_dir.as_posix())
    contexts: list[Any] = []

    def run_harness_process(
        context: Any,
        harness_registry: object,
        **kwargs: object,
    ) -> ProcessOutcome:
        _ = (harness_registry, kwargs)
        contexts.append(context)
        return ProcessOutcome(
            command=(),
            exit_code=0,
            chat_id="c45",
            primary_spawn_id="p45-continue",
            primary_started=0.0,
            primary_started_epoch=0.0,
            primary_started_local_iso=None,
            resolved_harness_session_id="session-45",
        )

    monkeypatch.setattr("meridian.lib.launch.process.run_harness_process", run_harness_process)

    _run_primary_continue(project_root, "p45")

    context = contexts[0]
    assert context.work_id is None
    assert context.binding.work_id is None
    assert "MERIDIAN_ACTIVE_WORK_ID" not in context.binding.environment.child_context_env
    assert context.task_cwd == source_task_dir


@pytest.mark.parametrize("replacement", ["missing", "file"])
def test_primary_continue_with_stale_work_task_dir_falls_back_without_mutating_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replacement: str,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    source_task_dir = tmp_path / "deleted-worktree"
    source_task_dir.mkdir()
    runtime_root = _state_root(project_root)
    project_state_dir = resolve_project_paths(project_root).root_dir
    work_repository.create_work_item(project_state_dir, "source-work")
    work_repository.update_work_item_task_dir(
        project_state_dir,
        "source-work",
        task_dir=source_task_dir.as_posix(),
    )
    _seed_primary_spawn(
        runtime_root,
        spawn_id="p46",
        harness_session_id="session-46",
        work_id="source-work",
        task_cwd=source_task_dir.as_posix(),
        launch_policy_snapshot=LaunchPolicySnapshot(model="gpt-5.3-codex", harness="codex"),
    )
    work_before = work_store.get_active_work_item(project_state_dir, "source-work")
    source_task_dir.rmdir()
    if replacement == "file":
        source_task_dir.write_text("replacement", encoding="utf-8")
    contexts: list[Any] = []
    real_bind_launch_context = launch_context.bind_launch_context

    def bind_launch_context(*args: Any, **kwargs: Any) -> Any:
        context = real_bind_launch_context(*args, **kwargs)
        contexts.append(context)
        return context

    monkeypatch.setattr(launch_context, "bind_launch_context", bind_launch_context)

    output = _run_primary_continue(project_root, "p46", dry_run=True)

    assert output.warning is not None
    assert source_task_dir.as_posix() in output.warning
    assert "falling back to the normal launch directory" in output.warning
    context = contexts[0]
    assert context.execution_cwd == project_root.resolve()
    assert context.task_cwd is None
    assert context.work_id == "source-work"
    assert context.binding.work_id == "source-work"
    work_after = work_store.get_active_work_item(project_state_dir, "source-work")
    assert work_after == work_before
    assert work_after is not None
    assert work_after.task_dir == source_task_dir.as_posix()


@pytest.mark.parametrize(
    ("overrides", "message_fragments"),
    [
        ({"passthrough": ("--custom",)}, ("--",)),
        ({"task_dir": "other-worktree"}, ("--continue", "--task-dir", "--fork")),
        ({"work": "other-work"}, ("--work", "--fork-fresh", "fresh session")),
    ],
)
def test_primary_continue_rejects_surface_overrides(
    tmp_path: Path,
    overrides: dict[str, object],
    message_fragments: tuple[str, ...],
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = _state_root(project_root)
    _seed_primary_spawn(
        runtime_root,
        spawn_id="p52",
        harness_session_id="session-52",
        launch_policy_snapshot=LaunchPolicySnapshot(model="gpt-5.3-codex", harness="codex"),
    )

    with pytest.raises(ValueError) as exc_info:
        _run_primary_continue(project_root, "p52", **overrides)

    message = str(exc_info.value)
    for fragment in message_fragments:
        assert fragment in message


def test_primary_continue_legacy_source_uses_persisted_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    source_task_dir = tmp_path / "legacy-worktree"
    source_task_dir.mkdir()
    runtime_root = _state_root(project_root)
    _seed_primary_spawn(
        runtime_root,
        spawn_id="p42",
        harness_session_id="session-42",
        work_id="legacy-work",
        task_cwd=source_task_dir.as_posix(),
        launch_policy_snapshot=None,
    )
    requests = _record_primary_launch(monkeypatch)

    _run_primary_continue(project_root, "p42")

    request = requests[0]
    assert request.launch_policy_snapshot is None
    assert request.work_id == "legacy-work"
    assert request.task_dir == source_task_dir.as_posix()
    assert request.session.source_execution_cwd == source_task_dir.as_posix()
    assert request.model == "gpt-5.3-codex"
    assert request.harness == "codex"


def test_primary_exact_continue_without_source_task_ignores_ambient_task_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    _state_root(project_root)
    ambient_task_dir = tmp_path / "ambient-task"
    ambient_task_dir.mkdir()
    monkeypatch.setenv("MERIDIAN_TASK_DIR", ambient_task_dir.as_posix())
    stub_bundle_request_and_resolve(
        monkeypatch,
        model="gpt-5.3-codex",
        model_token="gpt-5.3-codex",
        harness=HarnessId.CODEX,
        harness_model="openai/gpt-5.3-codex",
    )
    task_cwds: list[str | None] = []
    real_bind_launch_context = launch_context.bind_launch_context

    def bind_launch_context(*args: Any, **kwargs: Any) -> Any:
        context = real_bind_launch_context(*args, **kwargs)
        task_cwds.append(context.resolved_request.task_cwd)
        return context

    monkeypatch.setattr(launch_context, "bind_launch_context", bind_launch_context)

    launch_primary(
        project_root=project_root,
        request=LaunchRequest(
            model="gpt-5.3-codex",
            harness="codex",
            session_mode=SessionMode.RESUME,
            dry_run=True,
            session=SessionRequest(
                requested_harness_session_id="raw-session",
                continue_harness="codex",
                continue_source_ref="raw-session",
                continue_source_tracked=False,
            ),
        ),
        harness_registry=get_default_harness_registry(),
    )

    assert task_cwds[0] != ambient_task_dir.as_posix()
