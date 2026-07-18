from __future__ import annotations

from pathlib import Path

from meridian.cli.utils import missing_fork_session_error_with_discovery
from meridian.lib.ops.reference import resolve_session_reference
from meridian.lib.state import session_store, spawn_store
from meridian.lib.state.paths import resolve_project_runtime_root_for_write
from meridian.lib.state.primary_meta import PrimaryMetadata, write_primary_metadata


def _seed_pi_primary_family(tmp_path: Path) -> tuple[Path, Path, str, str, str]:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = resolve_project_runtime_root_for_write(project_root)
    runtime_root.mkdir(parents=True, exist_ok=True)
    custom_session_dir = tmp_path / "pi-sessions"
    custom_session_dir.mkdir()

    owner_chat_id = session_store.start_session(
        runtime_root,
        harness="pi",
        harness_session_id="ses-primary",
        model="openai-codex/gpt-5.4-mini",
        chat_id="c-owner",
        kind="primary",
    )
    primary_spawn_id = str(
        spawn_store.start_spawn(
            runtime_root,
            chat_id=owner_chat_id,
            owner_chat_id=owner_chat_id,
            model="openai-codex/gpt-5.4-mini",
            agent="coder",
            harness="pi",
            kind="primary",
            prompt="seed",
            harness_session_id="ses-primary",
        )
    )
    write_primary_metadata(
        runtime_root / "spawns" / primary_spawn_id,
        PrimaryMetadata(
            managed_backend=False,
            harness_session_id="ses-primary",
            session_dir=custom_session_dir.as_posix(),
        ),
    )

    child_spawn_id = str(
        spawn_store.start_spawn(
            runtime_root,
            chat_id="c-child",
            owner_chat_id=owner_chat_id,
            parent_id=primary_spawn_id,
            model="openai-codex/gpt-5.4-mini",
            agent="coder",
            harness="pi",
            kind="child",
            prompt="child",
            harness_session_id="",
        )
    )
    child_chat_id = session_store.start_session(
        runtime_root,
        harness="pi",
        harness_session_id="",
        model="openai-codex/gpt-5.4-mini",
        chat_id="c-child",
        spawn_id=child_spawn_id,
    )
    return project_root, runtime_root, owner_chat_id, child_chat_id, custom_session_dir.as_posix()


def test_resolve_chat_reference_for_pi_child_uses_owner_primary(tmp_path: Path) -> None:
    project_root, runtime_root, owner_chat_id, child_chat_id, session_dir = (
        _seed_pi_primary_family(tmp_path)
    )
    try:
        resolved = resolve_session_reference(
            project_root,
            child_chat_id,
            runtime_root=runtime_root,
        )
    finally:
        session_store.stop_session(runtime_root, child_chat_id)
        session_store.stop_session(runtime_root, owner_chat_id)

    assert resolved.source_pi_session_dir == session_dir


def test_pi_missing_session_diagnostics_from_child_ref(tmp_path: Path) -> None:
    project_root, runtime_root, owner_chat_id, child_chat_id, _session_dir = (
        _seed_pi_primary_family(tmp_path)
    )
    primary_spawn_id = next(
        record.id
        for record in spawn_store.list_spawns(runtime_root).records
        if record.kind == "primary"
    )
    write_primary_metadata(
        runtime_root / "spawns" / primary_spawn_id,
        PrimaryMetadata(
            managed_backend=False,
            harness_session_id=None,
            harness_session_discovery="never_created",
        ),
    )
    try:
        message = missing_fork_session_error_with_discovery(
            source_ref=child_chat_id,
            project_root=project_root,
            source_harness="pi",
            source_chat_id=child_chat_id,
        )
    finally:
        session_store.stop_session(runtime_root, child_chat_id)
        session_store.stop_session(runtime_root, owner_chat_id)

    assert "no Pi session" in message
    assert "ephemeral or never persisted" in message
