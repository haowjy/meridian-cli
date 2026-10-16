"""Exact-run and continuation reference boundaries over fabricated multi-run state."""

from __future__ import annotations

from pathlib import Path

import pytest

import meridian.lib.state.spawn.runs as spawn_runs
from meridian.lib.core.refs import parse_ref
from meridian.lib.ops.reference import (
    resolve_continue_source_row,
    resolve_continue_target,
    resolve_session_reference,
)
from meridian.lib.ops.session_target import resolve_transcript_source
from meridian.lib.ops.spawn.api import spawn_continue_sync
from meridian.lib.ops.spawn.models import SpawnContinueInput
from meridian.lib.state import session_store, spawn_store
from meridian.lib.state.paths import resolve_project_runtime_root_for_write
from tests.support.codex import write_codex_rollout
from tests.support.io_probe import IOProbe
from tests.support.spawn_runs import seed_run


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
        seed_run(
            runtime_root,
            key,
            bound=True,
            chat_id="c2",
            model="gpt-test",
            harness_session_id=native_id,
            native_store=native_store.as_posix(),
            prompt=f"run {key}",
        )
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

    seed_run(
        runtime_root,
        "p2.3",
        status="failed",
        chat_id="c-caller",
        owner_chat_id="c-caller",
        model="bad-model",
        prompt="fails before session scope",
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
        seed_run(
            runtime_root,
            key,
            status="failed",
            chat_id="c-caller",
            owner_chat_id="c-caller",
            model="bad",
            prompt="never started",
        )

    with pytest.raises(
        ValueError,
        match=r"Cannot continue spawn 'p8'; it never started a session, "
        r"so `--fork` is unavailable\.",
    ):
        resolve_session_reference(project_root, "p8", runtime_root=runtime_root)

    with pytest.raises(
        ValueError,
        match=r"Cannot continue spawn 'p8'; it never started a session, "
        r"so `--fork` is unavailable\.",
    ):
        resolve_continue_target(
            project_root,
            "p8.2",
            fork=True,
            runtime_root=runtime_root,
        )


def test_bare_continue_target_walks_only_its_unbound_run_tail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root = tmp_path / "runtime"
    run_count = 10
    bound = seed_run(
        runtime_root,
        "p7",
        bound=True,
        chat_id="c7",
        model="test",
        prompt="bound",
        harness_session_id="native-7",
    )
    for run in range(2, run_count + 1):
        seed_run(
            runtime_root,
            f"p7.{run}",
            status="failed",
            chat_id="c-failed",
            model="test",
            agent="",
            prompt="failed before session binding",
        )

    probe = IOProbe(monkeypatch)
    probe.watch("read_state", spawn_runs.read_state)

    row = resolve_continue_source_row(runtime_root, parse_ref("p7"))

    assert row is not None and row.id == bound
    assert probe.count("read_state") <= run_count + 1


def test_continue_refuses_an_exact_run_before_launch_preparation() -> None:
    with pytest.raises(ValueError, match=r"continue the spawn \(`p2`\).+--fork p2\.2"):
        spawn_continue_sync(SpawnContinueInput(spawn_id="p2.2", prompt=""))
