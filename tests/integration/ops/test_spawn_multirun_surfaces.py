"""Exact-run and continuation reference boundaries over fabricated multi-run state."""

from __future__ import annotations

from pathlib import Path

import pytest

import meridian.lib.state.spawn.runs as spawn_runs
from meridian.lib.core.refs import parse_ref
from meridian.lib.ops.reference import resolve_continue_source_row, resolve_session_reference
from meridian.lib.ops.session_target import resolve_transcript_source
from meridian.lib.ops.spawn.api import spawn_continue_sync
from meridian.lib.ops.spawn.models import SpawnContinueInput
from meridian.lib.state import session_store, spawn_store
from meridian.lib.state.paths import resolve_project_runtime_root_for_write
from tests.support.codex import write_codex_rollout
from tests.support.io_probe import IOProbe


def test_bare_ref_uses_latest_bound_run_while_run_one_alias_is_exact(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = resolve_project_runtime_root_for_write(project_root)
    runtime_root.mkdir(parents=True, exist_ok=True)
    native_store = tmp_path / "codex-sessions"
    native_id = "11111111-1111-4111-8111-111111111111"
    current_native_id = "22222222-2222-4222-8222-222222222222"
    write_codex_rollout(
        sessions_root=native_store, project_root=project_root,
        session_id=native_id, assistant_text="entry conversation",
    )
    write_codex_rollout(
        sessions_root=native_store, project_root=project_root,
        session_id=current_native_id, assistant_text="after clear",
    )

    for key in ("p2", "p2.2"):
        run = spawn_store.start_spawn(
            runtime_root, spawn_id=key, chat_id="c2", model="gpt-test",
            agent="coder", harness="codex", prompt=f"run {key}",
        )
        session_store.start_session(
            runtime_root, harness="codex", harness_session_id=native_id,
            model="gpt-test", chat_id="c2", native_store=native_store.as_posix(),
            spawn_id=run,
        )
        session = session_store.get_session_record(runtime_root, "c2")
        assert session is not None
        spawn_store.bind_run_generation(
            runtime_root,
            run,
            chat_id="c2",
            session_instance_id=session.session_instance_id,
            forked_from_history_id=session.forked_from_history_id,
        )
        session_store.stop_session(runtime_root, "c2")
        spawn_store.finalize_spawn(runtime_root, run, "succeeded", 0, origin="runner")
    current_chat = session_store.start_session(
        runtime_root,
        harness="codex",
        harness_session_id=current_native_id,
        model="gpt-test",
        chat_id="c3",
        native_store=native_store.as_posix(),
    )
    session_store.stop_session(runtime_root, current_chat)
    spawn_store.update_spawn(
        runtime_root,
        parse_ref("p2.2", row_key=True),
        current_chat_id=current_chat,
    )

    failed = spawn_store.start_spawn(
        runtime_root,
        spawn_id="p2.3",
        chat_id="c-caller",
        owner_chat_id="c-caller",
        model="bad-model",
        agent="coder",
        harness="codex",
        prompt="fails before session scope",
    )
    spawn_store.finalize_spawn(
        runtime_root,
        failed,
        "failed",
        1,
        origin="runner",
    )

    bare = resolve_session_reference(project_root, "p2", runtime_root=runtime_root)
    exact = resolve_session_reference(project_root, " p2.1 ", runtime_root=runtime_root)
    bare_log = resolve_transcript_source(
        "p2", project_root=project_root, runtime_root=runtime_root
    )
    exact_log = resolve_transcript_source(
        "p2.1", project_root=project_root, runtime_root=runtime_root
    )

    assert bare.source_spawn_id == "p2.2"
    assert bare.source_chat_id == "c3"
    assert bare.harness_session_id == "22222222-2222-4222-8222-222222222222"
    assert exact.source_spawn_id == "p2"
    assert exact.source_chat_id == "c2"
    assert bare_log.source.session_id == "22222222-2222-4222-8222-222222222222"
    assert exact_log.source.session_id == "11111111-1111-4111-8111-111111111111"


def test_multirun_never_bound_ref_refuses_instead_of_using_callers_chat(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = resolve_project_runtime_root_for_write(project_root)
    runtime_root.mkdir(parents=True, exist_ok=True)
    for key in ("p8", "p8.2"):
        run = spawn_store.start_spawn(
            runtime_root,
            spawn_id=key,
            chat_id="c-caller",
            owner_chat_id="c-caller",
            model="bad",
            agent="coder",
            harness="codex",
            prompt="never started",
        )
        spawn_store.finalize_spawn(runtime_root, run, "failed", 1, origin="runner")

    with pytest.raises(
        ValueError,
        match=r"Cannot continue spawn 'p8'; it never started a session, "
        r"so `--fork` is unavailable\.",
    ):
        resolve_session_reference(project_root, "p8", runtime_root=runtime_root)


@pytest.mark.parametrize("run_count", [10, 50])
def test_bare_continue_target_walks_only_its_unbound_run_tail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    run_count: int,
) -> None:
    runtime_root = tmp_path / "runtime"
    bound = spawn_store.start_spawn(
        runtime_root,
        spawn_id="p7",
        chat_id="c7",
        model="test",
        agent="",
        harness="codex",
        prompt="bound",
    )
    session_store.start_session(
        runtime_root,
        harness="codex",
        harness_session_id="native-7",
        model="test",
        chat_id="c7",
        spawn_id=bound,
    )
    session = session_store.get_session_record(runtime_root, "c7")
    assert session is not None
    spawn_store.bind_run_generation(
        runtime_root,
        bound,
        chat_id="c7",
        session_instance_id=session.session_instance_id,
        forked_from_history_id=session.forked_from_history_id,
    )
    session_store.stop_session(runtime_root, "c7")
    spawn_store.finalize_spawn(runtime_root, bound, "succeeded", 0, origin="runner")
    for run in range(2, run_count + 1):
        unbound = spawn_store.start_spawn(
            runtime_root,
            spawn_id=f"p7.{run}",
            chat_id="c-failed",
            model="test",
            agent="",
            harness="codex",
            prompt="failed before session binding",
        )
        spawn_store.finalize_spawn(
            runtime_root, unbound, "failed", 1, origin="runner",
        )

    probe = IOProbe(monkeypatch)
    probe.watch("read_state", spawn_runs.read_state)

    row = resolve_continue_source_row(runtime_root, parse_ref("p7"))

    assert row is not None and row.id == bound
    assert probe.count("read_state") <= run_count + 1


def test_continue_refuses_an_exact_run_before_launch_preparation() -> None:
    with pytest.raises(ValueError, match=r"continue the spawn \(`p2`\).+--fork p2\.2"):
        spawn_continue_sync(SpawnContinueInput(spawn_id="p2.2", prompt=""))
