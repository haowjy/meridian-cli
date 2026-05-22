from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from meridian.lib.ops.reference import resolve_session_reference
from meridian.lib.ops.spawn import api as spawn_api
from meridian.lib.ops.spawn.models import SpawnActionOutput, SpawnContinueInput
from meridian.lib.state import session_store
from meridian.lib.state.primary_meta import PrimaryMetadata, write_primary_metadata
from meridian.lib.state.spawn_store import start_spawn


def _runtime_root(tmp_path: Path) -> Path:
    runtime_root = tmp_path / ".meridian"
    runtime_root.mkdir(parents=True, exist_ok=True)
    return runtime_root


def _start_spawn_row(
    *,
    runtime_root: Path,
    control_root: Path | None,
    task_cwd: Path | None,
    execution_cwd: Path | None,
    harness_session_id: str = "thread-1",
) -> str:
    return str(
        start_spawn(
            runtime_root,
            chat_id="c1",
            model="gpt-5.4",
            agent="coder",
            harness="codex",
            prompt="hello",
            harness_session_id=harness_session_id,
            control_root=control_root.as_posix() if control_root is not None else None,
            task_cwd=task_cwd.as_posix() if task_cwd is not None else None,
            execution_cwd=execution_cwd.as_posix() if execution_cwd is not None else None,
        )
    )


@pytest.mark.parametrize("reference_mode", ["spawn", "chat"])
def test_resolve_tracked_references_prefer_persisted_control_root(
    tmp_path: Path,
    reference_mode: str,
) -> None:
    runtime_root = _runtime_root(tmp_path)
    current_control_root = tmp_path / "current-control"
    persisted_control_root = tmp_path / "persisted-control"
    task_cwd = tmp_path / "task-cwd"
    current_control_root.mkdir(parents=True, exist_ok=True)
    persisted_control_root.mkdir(parents=True, exist_ok=True)
    task_cwd.mkdir(parents=True, exist_ok=True)

    if reference_mode == "spawn":
        reference = _start_spawn_row(
            runtime_root=runtime_root,
            control_root=persisted_control_root,
            task_cwd=task_cwd,
            execution_cwd=task_cwd,
        )
        resolved = resolve_session_reference(
            current_control_root,
            reference,
            runtime_root=runtime_root,
        )
    else:
        chat_id = session_store.start_session(
            runtime_root,
            harness="codex",
            harness_session_id="thread-1",
            model="gpt-5.4",
            chat_id="c1",
            control_root=persisted_control_root.as_posix(),
            task_cwd=task_cwd.as_posix(),
            execution_cwd=task_cwd.as_posix(),
        )
        try:
            resolved = resolve_session_reference(
                current_control_root,
                chat_id,
                runtime_root=runtime_root,
            )
        finally:
            session_store.stop_session(runtime_root, chat_id)

    assert resolved.source_control_root == persisted_control_root.as_posix()
    assert resolved.source_execution_cwd == task_cwd.as_posix()


def test_resolve_harness_session_reference_prefers_persisted_control_root(tmp_path: Path) -> None:
    runtime_root = _runtime_root(tmp_path)
    current_control_root = tmp_path / "current-control"
    persisted_control_root = tmp_path / "persisted-control"
    task_cwd = tmp_path / "task-cwd"
    current_control_root.mkdir(parents=True, exist_ok=True)
    persisted_control_root.mkdir(parents=True, exist_ok=True)
    task_cwd.mkdir(parents=True, exist_ok=True)

    chat_id = session_store.start_session(
        runtime_root,
        harness="codex",
        harness_session_id="thread-raw-ref",
        model="gpt-5.4",
        chat_id="c1",
        control_root=persisted_control_root.as_posix(),
        task_cwd=task_cwd.as_posix(),
        execution_cwd=task_cwd.as_posix(),
    )
    try:
        resolved = resolve_session_reference(
            current_control_root,
            "thread-raw-ref",
            runtime_root=runtime_root,
        )
    finally:
        session_store.stop_session(runtime_root, chat_id)

    assert resolved.tracked is True
    assert resolved.source_chat_id == chat_id
    assert resolved.source_control_root == persisted_control_root.as_posix()
    assert resolved.source_execution_cwd == task_cwd.as_posix()


def test_spawn_continue_uses_persisted_source_control_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root = _runtime_root(tmp_path)
    current_control_root = tmp_path / "current-control"
    persisted_control_root = tmp_path / "persisted-control"
    task_cwd = tmp_path / "task-cwd"
    current_control_root.mkdir(parents=True, exist_ok=True)
    persisted_control_root.mkdir(parents=True, exist_ok=True)
    task_cwd.mkdir(parents=True, exist_ok=True)

    spawn_id = _start_spawn_row(
        runtime_root=runtime_root,
        control_root=persisted_control_root,
        task_cwd=task_cwd,
        execution_cwd=task_cwd,
    )
    captured: dict[str, Any] = {}

    monkeypatch.setattr(
        spawn_api,
        "_resolve_spawn_read_authority",
        lambda *, project_root, prepared=None: (current_control_root, runtime_root),
    )
    monkeypatch.setattr(
        spawn_api,
        "_resolve_effective_fork_target_harness",
        lambda *args, **kwargs: "codex",
    )

    def _fake_spawn_create_sync(
        create_input: Any,
        *,
        ctx: Any = None,
        sink: Any = None,
        prepared: Any = None,
    ) -> SpawnActionOutput:
        _ = (ctx, sink, prepared)
        captured["create_input"] = create_input
        captured["session"] = create_input.session
        return SpawnActionOutput(command="spawn.create", status="running", spawn_id="p2")

    monkeypatch.setattr(spawn_api, "spawn_create_sync", _fake_spawn_create_sync)

    spawn_api.spawn_continue_sync(
        SpawnContinueInput(
            spawn_id=spawn_id,
            prompt="Continue",
            worktree=True,
            project_root=current_control_root.as_posix(),
        )
    )

    assert captured["session"].source_control_root == persisted_control_root.as_posix()
    assert captured["create_input"].worktree is True



def test_resolve_spawn_reference_legacy_rows_fall_back_to_current_control_root(
    tmp_path: Path,
) -> None:
    runtime_root = _runtime_root(tmp_path)
    current_control_root = tmp_path / "current-control"
    legacy_task_cwd = tmp_path / "legacy-task-cwd"
    current_control_root.mkdir(parents=True, exist_ok=True)
    legacy_task_cwd.mkdir(parents=True, exist_ok=True)

    spawn_id = _start_spawn_row(
        runtime_root=runtime_root,
        control_root=None,
        task_cwd=None,
        execution_cwd=legacy_task_cwd,
    )

    resolved = resolve_session_reference(
        current_control_root,
        spawn_id,
        runtime_root=runtime_root,
    )

    assert resolved.source_control_root == current_control_root.as_posix()
    assert resolved.source_execution_cwd == legacy_task_cwd.as_posix()


def test_resolve_spawn_reference_for_pi_primary_includes_source_session_dir(
    tmp_path: Path,
) -> None:
    runtime_root = _runtime_root(tmp_path)
    current_control_root = tmp_path / "current-control"
    current_control_root.mkdir(parents=True, exist_ok=True)
    custom_session_dir = tmp_path / "custom-pi-sessions"

    spawn_id = str(
        start_spawn(
            runtime_root,
            chat_id="c-pi-primary",
            model="openai-codex/gpt-5.4-mini",
            agent="coder",
            harness="pi",
            kind="primary",
            prompt="seed",
            harness_session_id="ses-pi-primary",
        )
    )
    spawn_dir = runtime_root / "spawns" / spawn_id
    spawn_dir.mkdir(parents=True, exist_ok=True)
    write_primary_metadata(
        spawn_dir,
        PrimaryMetadata(
            managed_backend=False,
            harness_session_id="ses-pi-primary",
            session_dir=custom_session_dir.as_posix(),
        ),
    )

    resolved = resolve_session_reference(
        current_control_root,
        spawn_id,
        runtime_root=runtime_root,
    )

    assert resolved.source_pi_session_dir == custom_session_dir.as_posix()


def test_resolve_chat_reference_for_pi_includes_source_session_dir(
    tmp_path: Path,
) -> None:
    runtime_root = _runtime_root(tmp_path)
    current_control_root = tmp_path / "current-control"
    current_control_root.mkdir(parents=True, exist_ok=True)
    custom_session_dir = tmp_path / "custom-pi-sessions"

    chat_id = session_store.start_session(
        runtime_root,
        harness="pi",
        harness_session_id="ses-pi-chat",
        model="openai-codex/gpt-5.4-mini",
        chat_id="c77",
    )
    try:
        spawn_id = str(
            start_spawn(
                runtime_root,
                chat_id=chat_id,
                model="openai-codex/gpt-5.4-mini",
                agent="coder",
                harness="pi",
                kind="primary",
                prompt="seed",
                harness_session_id="ses-pi-chat",
            )
        )
        spawn_dir = runtime_root / "spawns" / spawn_id
        spawn_dir.mkdir(parents=True, exist_ok=True)
        write_primary_metadata(
            spawn_dir,
            PrimaryMetadata(
                managed_backend=False,
                harness_session_id="ses-pi-chat",
                session_dir=custom_session_dir.as_posix(),
            ),
        )

        resolved = resolve_session_reference(
            current_control_root,
            chat_id,
            runtime_root=runtime_root,
        )
    finally:
        session_store.stop_session(runtime_root, chat_id)

    assert resolved.source_pi_session_dir == custom_session_dir.as_posix()
