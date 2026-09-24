"""Primary ``--continue`` adapter coverage.

Shared exact-continue semantics belong to ``test_continue_replay.py``. These
integration tests cover primary CLI validation, reference resolution, and the
handoff to the launch layer.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
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
from meridian.lib.launch.request import SessionRequest, SpawnRequest
from meridian.lib.launch.types import SessionMode
from meridian.lib.ops.reference import UntrackedSourceUse
from meridian.lib.state import session_authority as native_authority
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


def _write_v4_pin(runtime_root: Path) -> None:
    key = native_authority.NativeSessionKey(
        harness="pi", store="/synthetic/pi", native_session_id="native-conversation"
    )
    begin = native_authority.BeginEventV4(
        run_id="run",
        attempt_id="attempt",
        transport_scope_id="transport",
        harness="pi",
        store=key.store,
        operation="fresh",
        attempt_number=1,
    )
    fact = native_authority.BoundaryFactV4(
        run_id="run",
        attempt_id="attempt",
        boundary="entry",
        key=key,
        evidence=native_authority.BoundaryEvidence(
            transport_scope_id="transport",
            order=1,
            correlation="entry",
            selection=native_authority.CreatedSelection(creation_request="fresh"),
        ),
        file=native_authority.QualifiedLocalFile(
            kind="local_file",
            path="/synthetic/pi/native-conversation.jsonl",
            store_object={"device": 1, "inode": 10},
            file_object={"device": 1, "inode": 11},
            rule="pi-session-file:v1",
        ),
    )
    builder = native_authority._JournalBuilder()
    native_authority.fold_row(builder, begin)
    transition = native_authority.plan_attempt(builder.attempt_view(), builder.identity(), fact)
    if isinstance(transition, native_authority.NeedChat):
        transition = native_authority.plan_attempt(
            builder.attempt_view(), builder.identity(), fact, assigned_chat="c1"
        )
    assert isinstance(transition, native_authority.AttemptTransition)
    runtime_root.mkdir(parents=True, exist_ok=True)
    (runtime_root / "sessions.jsonl").write_text(
        f"{begin.model_dump_json()}\n{transition.row.model_dump_json()}\n",
        encoding="utf-8",
    )


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
    session_store.start_session(
        runtime_root,
        chat_id="c-primary",
        spawn_id=spawn_id,
        harness=snapshot.harness if snapshot is not None else "codex",
        harness_session_id=harness_session_id or "",
        model=snapshot.model if snapshot is not None else "gpt-5.3-codex",
    )
    session_store.stop_session(runtime_root, "c-primary")
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


def test_explicit_fresh_conflict_refuses_before_lookup_or_non_dry_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit fresh is an assertion, not the default-mode sentinel."""
    project_root = tmp_path / "repo"
    project_root.mkdir()
    _state_root(project_root)
    lookups: list[str] = []
    work_effects: list[str] = []

    import meridian.lib.launch as launch_module
    import meridian.lib.ops.reference as reference_ops

    monkeypatch.setattr(
        reference_ops,
        "resolve_source_use",
        lambda *args, **kwargs: lookups.append("lookup"),
    )
    monkeypatch.setattr(
        launch_module,
        "_resolve_work_id_for_launch",
        lambda *args, **kwargs: work_effects.append("work") or None,
    )

    with pytest.raises(ValueError, match="conflicting operation facts"):
        launch_primary(
            project_root=project_root,
            harness_registry=get_default_harness_registry(),
            request=LaunchRequest(
                harness="opencode",
                session_mode=SessionMode.FRESH,
                dry_run=False,
                session=SessionRequest(
                    continue_source_ref="native-A",
                    requested_harness_session_id="native-A",
                    primary_session_mode="resume",
                ),
            ),
        )

    assert lookups == []
    assert work_effects == []


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
        "model": None,
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


@pytest.mark.parametrize(
    ("ref", "operation"),
    [
        ("c1", "resume"),
        ("native-conversation", "resume"),
        ("p1", "fork"),
        ("p1", "fork-fresh"),
        ("c1", "fork"),
        ("c1", "fork-fresh"),
    ],
)
def test_primary_source_use_aliases_refuse_before_launch(
    tmp_path: Path,
    ref: str,
    operation: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = _state_root(project_root)
    if ref == "p1":
        _seed_primary_spawn(
            runtime_root,
            spawn_id="p1",
            harness_session_id="native-conversation",
        )
    else:
        _write_v4_pin(runtime_root)
    kwargs: dict[str, object] = {
        "continue_ref": ref if operation == "resume" else None,
        "fork_ref": ref if operation == "fork" else None,
        "fork_fresh_ref": ref if operation == "fork-fresh" else None,
        "model": None,
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
        "dry_run": True,
        "passthrough": (),
        "skills": (),
        "project_root": project_root,
    }
    expected_reason = "tracked_run_unresolved" if ref == "p1" else "transport_unqualified"

    real_launch_primary = primary_launch_module.launch_primary
    entered_owner = False

    def enter_real_owner(**launch_kwargs: object) -> Any:
        nonlocal entered_owner
        entered_owner = True
        return real_launch_primary(**launch_kwargs)

    monkeypatch.setattr(primary_launch_module, "launch_primary", enter_real_owner)
    with pytest.raises(ValueError, match=expected_reason):
        run_primary_launch(**cast("Any", kwargs))
    assert entered_owner


def test_direct_launch_revalidates_source_before_native_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import meridian.lib.launch.continue_replay as continue_replay
    import meridian.lib.ops.reference as reference

    project_root = tmp_path / "repo"
    project_root.mkdir()
    _write_v4_pin(_state_root(project_root))

    def fail_if_native_resolution_runs(*args: object, **kwargs: object) -> None:
        _ = (args, kwargs)
        pytest.fail("native session resolution ran before launch-owner authorization")

    monkeypatch.setattr(
        "meridian.lib.ops.reference.resolve_session_reference", fail_if_native_resolution_runs
    )
    native_reads: list[object] = []
    observations: list[object] = []
    detections: list[object] = []
    monkeypatch.setattr(
        continue_replay,
        "read_last_executed_model",
        lambda *args, **kwargs: native_reads.append(args),
    )
    monkeypatch.setattr(
        continue_replay,
        "record_model_observation",
        lambda *args, **kwargs: observations.append(args),
    )
    monkeypatch.setattr(
        reference,
        "infer_harness_from_untracked_session_ref",
        lambda *args, **kwargs: detections.append(args),
    )

    with pytest.raises(ValueError, match="transport_unqualified"):
        launch_primary(
            project_root=project_root,
            request=LaunchRequest(
                dry_run=True,
                harness="pi",
                session_mode=SessionMode.RESUME,
                session=SessionRequest(
                    requested_harness_session_id="native-conversation",
                    continue_source_tracked=False,
                    continue_source_ref="c1",
                ),
            ),
            harness_registry=get_default_harness_registry(),
        )
    assert native_reads == []
    assert observations == []
    assert detections == []


@pytest.mark.parametrize("source_ref", ["unknown-native", " "])
def test_direct_launch_rejects_mismatched_source_description(
    tmp_path: Path,
    source_ref: str,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    _write_v4_pin(_state_root(project_root))

    with pytest.raises(ValueError, match="source-use authorization refused"):
        launch_primary(
            project_root=project_root,
            request=LaunchRequest(
                dry_run=True,
                harness="pi",
                session=SessionRequest(
                    requested_harness_session_id="native-conversation",
                    primary_session_mode="resume",
                    continue_source_ref=source_ref,
                    continue_source_tracked=False,
                ),
            ),
            harness_registry=get_default_harness_registry(),
        )


@pytest.mark.parametrize(
    ("resolved_id", "resolved_harness", "tracked"),
    [
        ("native-B", "h1", False),
        (None, "h1", False),
        ("", "h1", False),
        (" ", "h1", False),
        ("native-A", "h2", False),
        ("native-A", "h1", True),
    ],
)
def test_primary_resolver_conflict_stops_before_replay_and_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    resolved_id: str | None,
    resolved_harness: str,
    tracked: bool,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = _state_root(project_root)
    import meridian.lib.launch.continue_replay as continue_replay
    import meridian.lib.launch.source_selection as source_selection
    import meridian.lib.ops.reference as reference

    monkeypatch.setattr(
        source_selection,
        "validate_primary_source_use",
        lambda **kwargs: UntrackedSourceUse(
            operation="resume",
            original_ref="native-A",
            native_id="native-A",
            harness="h1",
            lookup_scope=runtime_root,
        ),
    )
    monkeypatch.setattr(
        reference,
        "resolve_session_reference",
        lambda *args, **kwargs: SimpleNamespace(
            authoritative_harness_session_id=resolved_id,
            harness=resolved_harness,
            tracked=tracked,
        ),
    )
    replay_calls: list[str] = []
    model_reads: list[str] = []
    observations: list[str] = []
    work_materializations: list[str] = []
    monkeypatch.setattr(
        continue_replay,
        "build_continue_replay_contract",
        lambda **kwargs: replay_calls.append("contract"),
    )
    monkeypatch.setattr(
        continue_replay,
        "read_last_executed_model",
        lambda *args, **kwargs: model_reads.append("read"),
    )
    monkeypatch.setattr(
        continue_replay,
        "record_model_observation",
        lambda *args, **kwargs: observations.append("write"),
    )
    monkeypatch.setattr(
        "meridian.lib.launch._resolve_work_id_for_launch",
        lambda *args, **kwargs: work_materializations.append("work"),
    )

    with pytest.raises(ValueError, match="source selection conflict"):
        launch_primary(
            project_root=project_root,
            request=LaunchRequest(
                dry_run=True,
                harness="h1",
                session_mode=SessionMode.RESUME,
                session=SessionRequest(
                    requested_harness_session_id="native-A",
                    continue_source_ref="native-A",
                    primary_session_mode="resume",
                ),
            ),
            harness_registry=get_default_harness_registry(),
        )

    assert replay_calls == []
    assert model_reads == []
    assert observations == []
    assert work_materializations == []


@pytest.mark.parametrize(
    ("source_ref", "native_id"),
    [("native-A", "native-B"), (" ", "native-A")],
)
def test_primary_original_source_conflict_stops_before_strict_lookup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source_ref: str,
    native_id: str,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    import meridian.lib.launch.source_selection as source_selection

    monkeypatch.setattr(
        source_selection,
        "validate_primary_source_use",
        lambda **kwargs: pytest.fail("conflicting source should not query authority"),
    )
    with pytest.raises(ValueError, match="source selection conflict"):
        launch_primary(
            project_root=project_root,
            request=LaunchRequest(
                dry_run=True,
                harness="h1",
                session_mode=SessionMode.RESUME,
                session=SessionRequest(
                    requested_harness_session_id=native_id,
                    continue_source_ref=source_ref,
                    primary_session_mode="resume",
                ),
            ),
            harness_registry=get_default_harness_registry(),
        )


@pytest.mark.parametrize(
    "session",
    [
        SessionRequest(
            continue_source_ref="native-A",
            primary_session_mode="fork",
        ),
        SessionRequest(
            continue_source_ref="native-A",
            continue_fork=True,
            primary_session_mode="resume",
        ),
    ],
)
def test_conflicting_primary_mode_facts_refuse_before_strict_lookup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    session: SessionRequest,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    import meridian.lib.launch.source_selection as source_selection

    monkeypatch.setattr(
        source_selection,
        "validate_primary_source_use",
        lambda **kwargs: pytest.fail("conflicting modes must not query authority"),
    )
    with pytest.raises(ValueError, match="conflicting operation facts"):
        launch_primary(
            project_root=project_root,
            request=LaunchRequest(
                dry_run=True,
                harness="h1",
                session_mode=SessionMode.RESUME,
                session=session,
            ),
            harness_registry=get_default_harness_registry(),
        )


def test_replay_selector_change_refuses_before_work_or_preparation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = _state_root(project_root)
    import meridian.lib.launch.continue_replay as continue_replay
    import meridian.lib.launch.source_selection as source_selection
    import meridian.lib.ops.reference as reference

    monkeypatch.setattr(
        source_selection,
        "validate_primary_source_use",
        lambda **kwargs: UntrackedSourceUse(
            operation="resume",
            original_ref="native-A",
            native_id="native-A",
            harness="h1",
            lookup_scope=runtime_root,
        ),
    )
    monkeypatch.setattr(
        reference,
        "resolve_session_reference",
        lambda *args, **kwargs: SimpleNamespace(
            authoritative_harness_session_id="native-A",
            harness="h1",
            tracked=False,
            source_launch_policy_snapshot=None,
            missing_harness_session_id=False,
            source_chat_id="c1",
            source_history_id=None,
            source_model=None,
            source_agent=None,
            source_skills=(),
            source_work_id=None,
            source_execution_cwd=None,
            source_control_root=None,
            source_claude_config_dir=None,
            source_pi_session_dir=None,
        ),
    )
    monkeypatch.setattr(
        continue_replay,
        "build_continue_replay_contract",
        lambda **kwargs: SimpleNamespace(
            session=SessionRequest(
                requested_harness_session_id="native-B",
                continue_source_ref="native-A",
                continue_harness="h1",
                primary_session_mode="resume",
            ),
            task_dir=None,
            harness="h1",
            launch_policy_snapshot=None,
        ),
    )
    later_calls: list[str] = []
    monkeypatch.setattr(
        "meridian.lib.launch._resolve_work_id_for_launch",
        lambda *args, **kwargs: later_calls.append("work"),
    )
    with pytest.raises(ValueError, match="source selection changed"):
        launch_primary(
            project_root=project_root,
            request=LaunchRequest(
                dry_run=True,
                harness="h1",
                session_mode=SessionMode.RESUME,
                session=SessionRequest(
                    requested_harness_session_id="native-A",
                    continue_source_ref="native-A",
                    primary_session_mode="resume",
                ),
            ),
            harness_registry=get_default_harness_registry(),
        )
    assert later_calls == []


def test_fork_resolver_snapshot_harness_conflict_refuses_before_preparation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = _state_root(project_root)
    import meridian.lib.launch.source_selection as source_selection
    import meridian.lib.ops.reference as reference

    monkeypatch.setattr(
        source_selection,
        "validate_primary_source_use",
        lambda **kwargs: UntrackedSourceUse(
            operation="fork",
            original_ref="native-A",
            native_id="native-A",
            harness="h1",
            lookup_scope=runtime_root,
        ),
    )
    monkeypatch.setattr(
        reference,
        "resolve_session_reference",
        lambda *args, **kwargs: SimpleNamespace(
            authoritative_harness_session_id="native-A",
            harness="h1",
            tracked=False,
            source_launch_policy_snapshot=LaunchPolicySnapshot(
                model="model", harness="h2"
            ),
            missing_harness_session_id=False,
        ),
    )
    with pytest.raises(ValueError, match="source harness changed"):
        launch_primary(
            project_root=project_root,
            request=LaunchRequest(
                dry_run=True,
                harness="h1",
                session_mode=SessionMode.FORK,
                session=SessionRequest(
                    requested_harness_session_id="native-A",
                    continue_source_ref="native-A",
                    primary_session_mode="fork",
                ),
            ),
            harness_registry=get_default_harness_registry(),
        )


@pytest.mark.parametrize(
    "selector_args",
    [
        ("--continue",),
        ("-c",),
        ("--resume",),
        ("-r",),
        ("--session-id", "native-conversation"),
        ("--session-id=native-conversation",),
        ("--session", "native-conversation"),
        ("--fork", "native-conversation"),
    ],
)
def test_primary_pi_dry_run_rejects_raw_native_selector_flags(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    selector_args: tuple[str, ...],
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    _state_root(project_root)

    with pytest.raises(ValueError, match="native-session selectors"):
        launch_primary(
            project_root=project_root,
            request=LaunchRequest(
                dry_run=True,
                harness="pi",
                passthrough_args=selector_args,
            ),
            harness_registry=get_default_harness_registry(),
        )


def test_primary_from_remains_fresh_and_does_not_use_source_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    from meridian.lib.state import session_store

    requests = _record_primary_launch(monkeypatch)
    monkeypatch.setattr(
        session_store,
        "read_native_source_use_snapshot",
        lambda *args, **kwargs: pytest.fail("--from must not enter source-use"),
    )

    run_primary_launch(
        project_root=project_root,
        continue_ref=None,
        fork_ref=None,
        fork_fresh_ref=None,
        from_ref="c1",
        model=None,
        harness="pi",
        agent=None,
        work="",
        yolo=False,
        approval=None,
        autocompact=None,
        effort=None,
        sandbox=None,
        timeout=None,
        dry_run=True,
        passthrough=(),
        skills=(),
    )

    assert requests[0].session.primary_session_mode is None
    assert requests[0].session.continue_source_ref is None


@pytest.mark.parametrize(
    "raw_args",
    [
        ("--system-prompt", "raw"),
        ("--system-prompt=raw",),
        ("--append-system-prompt", "first", "--append-system-prompt=second"),
    ],
)
def test_tracked_replayed_raw_system_flags_refuse_before_normalization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    raw_args: tuple[str, ...],
) -> None:
    monkeypatch.setattr(launch_context, "_build_shared_composition", lambda **kwargs: None)
    request = SpawnRequest(
        prompt="task",
        harness="pi",
        session=SessionRequest(continue_source_ref="c1"),
        launch_policy_snapshot=LaunchPolicySnapshot(
            model="test", harness="pi", extra_args=raw_args
        ),
    )
    policy = SimpleNamespace(profile=None, adapter=object(), resolved_skills=())

    with pytest.raises(ValueError, match="before system-prompt normalization"):
        launch_context._resolve_primary_projection(
            request=request,
            project_paths=launch_context.ProjectConfigPaths(
                project_root=tmp_path, execution_cwd=tmp_path
            ),
            active_work_dir=None,
            policy=policy,
            resolved_continue_harness_session_id="native-id",
        )


def _record_primary_launch(monkeypatch: pytest.MonkeyPatch) -> list[LaunchRequest]:
    requests: list[LaunchRequest] = []

    def launch_primary(
        *,
        project_root: Path,
        request: LaunchRequest,
        harness_registry: object,
    ) -> LaunchResult:
        _ = harness_registry
        if request.session.continue_source_ref is not None:
            from meridian.lib.launch import _resolve_primary_source_request

            request = _resolve_primary_source_request(
                request=request,
                project_root=project_root,
                harness_registry=get_default_harness_registry(),
            )
        requests.append(request)
        return LaunchResult(
            command=(),
            exit_code=0,
            continue_ref=request.session.requested_harness_session_id,
            continue_chat_id=request.session.continue_chat_id,
            primary_source_chat_id=request.primary_source_chat_id,
            primary_source_warning=request.primary_source_warning,
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
    with pytest.raises(ValueError, match="tracked_run_unresolved"):
        _run_primary_continue(project_root, "p41")


def test_primary_continue_rejects_tracked_replay_before_work_materialization(
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
    work_effects: list[str] = []
    import meridian.lib.launch as launch_module

    monkeypatch.setattr(
        launch_module,
        "_resolve_work_id_for_launch",
        lambda *args, **kwargs: work_effects.append("work") or None,
    )

    try:
        with pytest.raises(ValueError, match="Primary source selection conflict"):
            _run_primary_continue(project_root, spawn_chat_id)
    finally:
        session_store.stop_session(runtime_root, spawn_chat_id)

    assert work_effects == []


def test_primary_continue_does_not_inherit_ambient_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub_bundle_request_and_resolve(monkeypatch, model="gpt-5.3-codex", harness=HarnessId.CODEX)
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
    maintained: list[str] = []

    def maintain_history(project: Path, primary_spawn_id: str) -> None:
        assert project == project_root
        maintained.append(primary_spawn_id)

    monkeypatch.setattr(
        "meridian.lib.ops.session_archive.session_stop_maintenance", maintain_history
    )

    with pytest.raises(ValueError, match="tracked_run_unresolved"):
        _run_primary_continue(project_root, "p45")
    assert maintained == []
    assert contexts == []


@pytest.mark.parametrize("replacement", ["missing", "file"])
def test_primary_continue_with_stale_work_task_dir_falls_back_without_mutating_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replacement: str,
) -> None:
    stub_bundle_request_and_resolve(monkeypatch, model="gpt-5.3-codex", harness=HarnessId.CODEX)
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
    real_bind_launch_context = launch_context._bind_launch_context_impl

    def bind_launch_context(*args: Any, **kwargs: Any) -> Any:
        context = real_bind_launch_context(*args, **kwargs)
        contexts.append(context)
        return context

    monkeypatch.setattr(launch_context, "_bind_launch_context_impl", bind_launch_context)

    with pytest.raises(ValueError, match="tracked_run_unresolved"):
        _run_primary_continue(project_root, "p46", dry_run=True)
    assert contexts == []
    assert work_store.get_active_work_item(project_state_dir, "source-work") == work_before


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
    with pytest.raises(ValueError, match="tracked_run_unresolved"):
        _run_primary_continue(project_root, "p42")


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
    real_bind_launch_context = launch_context._bind_launch_context_impl

    def bind_launch_context(*args: Any, **kwargs: Any) -> Any:
        context = real_bind_launch_context(*args, **kwargs)
        task_cwds.append(context.resolved_request.task_cwd)
        return context

    monkeypatch.setattr(launch_context, "_bind_launch_context_impl", bind_launch_context)

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


def test_fork_old_harness_generation_preserves_selected_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    root = _state_root(project_root)
    source = session_store.start_session(
        root,
        harness="codex",
        harness_session_id="older-native",
        model="gpt-5.3-codex",
        kind="primary",
    )
    first = spawn_store.start_spawn(
        root,
        chat_id=source,
        model="gpt-5.3-codex",
        agent="",
        harness="codex",
        prompt="old",
        kind="primary",
    )
    session_store.update_session_spawn_id(root, source, first)
    original = spawn_store.get_spawn(root, first)
    session_store.stop_session(root, source)
    source = session_store.start_session(
        root,
        chat_id=source,
        harness="codex",
        harness_session_id="newer-native",
        model="gpt-5.3-codex",
        kind="primary",
    )
    second = spawn_store.start_spawn(
        root,
        chat_id=source,
        model="gpt-5.3-codex",
        agent="",
        harness="codex",
        prompt="new",
        kind="primary",
    )
    session_store.update_session_spawn_id(root, source, second)
    session_store.stop_session(root, source)
    with pytest.raises(ValueError, match="native_claim_blocked"):
        _run_primary_continue(
            project_root, continue_ref=None, fork_ref="older-native", dry_run=True
        )
    assert original is not None


def _opencode_continue_spec_model(
    *,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    selection_source: str,
) -> str | None:
    from meridian.lib.state import session_store
    from meridian.lib.state.session_store import ConversationModelSelection

    project_root = tmp_path / "repo"
    project_root.mkdir()
    _state_root(project_root)
    snapshot_reads = 0
    read_snapshot = session_store.read_native_source_use_snapshot

    def count_snapshot_reads(*args: Any, **kwargs: Any) -> Any:
        nonlocal snapshot_reads
        snapshot_reads += 1
        return read_snapshot(*args, **kwargs)

    monkeypatch.setattr(session_store, "read_native_source_use_snapshot", count_snapshot_reads)
    stub_bundle_request_and_resolve(
        monkeypatch,
        model="deepseek/deepseek-flash",
        model_token="deepseek-flash",
        harness=HarnessId.OPENCODE,
        harness_model="deepseek/deepseek-flash",
    )
    spec_models: list[str | None] = []
    real_bind_launch_context = launch_context._bind_launch_context_impl

    def bind_launch_context(*args: Any, **kwargs: Any) -> Any:
        context = real_bind_launch_context(*args, **kwargs)
        spec_models.append(context.binding.spec.model)
        return context

    monkeypatch.setattr(launch_context, "_bind_launch_context_impl", bind_launch_context)

    launch_primary(
        project_root=project_root,
        request=LaunchRequest(
            model="deepseek/deepseek-flash",
            harness="opencode",
            session_mode=SessionMode.RESUME,
            dry_run=True,
            session=SessionRequest(
                requested_harness_session_id="raw-session",
                continue_harness="opencode",
                continue_source_ref="raw-session",
                continue_source_tracked=False,
                conversation_intent=ConversationModelSelection(
                    requested_token="deepseek/deepseek-flash",
                    selected_token="deepseek",
                    canonical_model_id="deepseek-flash",
                    harness_model_id="deepseek/deepseek-flash",
                    model_mode="named",
                    selection_source=cast("Any", selection_source),
                ),
            ),
        ),
        harness_registry=get_default_harness_registry(),
    )

    assert spec_models
    assert snapshot_reads == 1
    return spec_models[0]



def test_public_bind_refuses_forged_prepared_source_without_transferable_proof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dataclasses import replace

    from meridian.lib.launch.context import RuntimeBindings
    from meridian.lib.state.paths import resolve_project_runtime_root
    from meridian.lib.state.session_store import ConversationModelSelection

    project_root = tmp_path / "repo"
    project_root.mkdir()
    _state_root(project_root)
    stub_bundle_request_and_resolve(
        monkeypatch,
        model="deepseek/deepseek-flash",
        model_token="deepseek-flash",
        harness=HarnessId.OPENCODE,
        harness_model="deepseek/deepseek-flash",
    )
    prepared_values: list[tuple[Any, Any]] = []
    real_prepare = launch_context.prepare_launch_surface

    def capture_prepare(*args: Any, **kwargs: Any) -> Any:
        prepared = real_prepare(*args, **kwargs)
        prepared_values.append((prepared, kwargs["runtime"]))
        return prepared

    monkeypatch.setattr(launch_context, "prepare_launch_surface", capture_prepare)
    launch_primary(
        project_root=project_root,
        request=LaunchRequest(
            model="deepseek/deepseek-flash",
            harness="opencode",
            session_mode=SessionMode.RESUME,
            dry_run=True,
            session=SessionRequest(
                requested_harness_session_id="raw-session",
                continue_harness="opencode",
                continue_source_ref="raw-session",
                conversation_intent=ConversationModelSelection(
                    requested_token="deepseek-flash",
                    selected_token="deepseek",
                    canonical_model_id="deepseek-flash",
                    harness_model_id="deepseek/deepseek-flash",
                    model_mode="named",
                    selection_source="explicit_override",
                ),
            ),
        ),
        harness_registry=get_default_harness_registry(),
    )
    assert len(prepared_values) == 1
    prepared, runtime = prepared_values[0]
    assert not hasattr(prepared, "primary_source_check")

    _write_v4_pin(resolve_project_runtime_root(project_root))
    forged_session = prepared.request.session.model_copy(
        update={
            "continue_source_ref": "c1",
            "requested_harness_session_id": "native-conversation",
            "continue_source_tracked": False,
            "recorded_native_source": None,
        }
    )
    pi_harness = get_default_harness_registry().get_subprocess_harness(HarnessId.PI)
    forged = replace(
        prepared,
        harness=pi_harness,
        request=prepared.request.model_copy(
            update={
                "harness": "pi",
                "session": forged_session,
            }
        ),
    )
    with pytest.raises(ValueError, match="source-use authorization refused"):
        launch_context.bind_launch_context(
            prepared=forged,
            bindings=RuntimeBindings(spawn_id="forged"),
            runtime=runtime,
            project_root=project_root,
            harness_registry=get_default_harness_registry(),
        )

def test_opencode_exact_continue_explicitly_requested_model_stays_in_spec(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert (
        _opencode_continue_spec_model(
            tmp_path=tmp_path,
            monkeypatch=monkeypatch,
            selection_source="explicit_override",
        )
        == "deepseek/deepseek-flash"
    )


def test_opencode_exact_continue_explicit_override_keeps_model_in_spec(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert (
        _opencode_continue_spec_model(
            tmp_path=tmp_path,
            monkeypatch=monkeypatch,
            selection_source="explicit_override",
        )
        == "deepseek/deepseek-flash"
    )


def test_primary_handler_preserves_observed_model_provenance_through_bind(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import meridian.lib.launch.continue_replay as continue_replay

    project_root = tmp_path / "repo"
    project_root.mkdir()
    _state_root(project_root)
    stub_bundle_request_and_resolve(
        monkeypatch,
        model="deepseek/deepseek-flash",
        model_token="deepseek-flash",
        harness=HarnessId.OPENCODE,
        harness_model="deepseek/deepseek-flash",
    )
    monkeypatch.setattr(
        continue_replay, "read_last_executed_model", lambda *args, **kwargs: "deepseek-flash"
    )
    monkeypatch.setattr(
        continue_replay, "_observed_model_routes_to_harness", lambda *args: True
    )
    bound_contexts: list[Any] = []
    real_bind = launch_context._bind_launch_context_impl

    def capture_bind(*args: Any, **kwargs: Any) -> Any:
        context = real_bind(*args, **kwargs)
        bound_contexts.append(context)
        return context

    monkeypatch.setattr(launch_context, "_bind_launch_context_impl", capture_bind)
    output = _run_primary_continue(
        project_root, "raw-session", harness="opencode", dry_run=True
    )

    assert output.message == "Resume dry-run."
    assert bound_contexts
    session = bound_contexts[-1].resolved_request.session
    assert session.conversation_intent is not None
    assert session.conversation_intent.selection_source == "observed_last_used"
    assert bound_contexts[-1].binding.spec.model is None


def test_primary_handler_rejects_exact_continue_agent_opt_out(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    _state_root(project_root)

    with pytest.raises(ValueError, match="agent opt-out"):
        _run_primary_continue(
            project_root,
            "raw-session",
            harness="opencode",
            agent="",
            dry_run=True,
        )


@pytest.mark.parametrize("flag", ["fork_ref", "fork_fresh_ref"])
def test_bare_fork_inference_authorizes_resolved_spawn_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    flag: str,
) -> None:
    from meridian.cli.argv_normalization import SELF_FORK_REF_SENTINEL

    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = _state_root(project_root)
    _seed_primary_spawn(runtime_root, spawn_id="p999", harness_session_id="native-999")
    monkeypatch.setenv("MERIDIAN_SPAWN_ID", "p999")

    with pytest.raises(ValueError, match="tracked_run_unresolved"):
        _run_primary_continue(
            project_root,
            continue_ref=None,
            harness=None,
            dry_run=True,
            **{flag: SELF_FORK_REF_SENTINEL},
        )
