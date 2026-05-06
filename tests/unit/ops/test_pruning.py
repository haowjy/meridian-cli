from __future__ import annotations

import os
from pathlib import Path

import pytest

from meridian.lib.harness.claude_preflight import (
    MERIDIAN_ORIGINAL_CLAUDE_CONFIG_DIR_ENV,
    ClaudeOverlayCleanupResult,
    ClaudeOverlayMaterializationResult,
    prepare_isolated_claude_config,
)
from meridian.lib.ops.pruning import (
    prune_orphan_project_dirs,
    prune_stale_claude_overlays,
    prune_stale_spawn_artifacts,
    scan_orphan_project_dirs,
    scan_stale_claude_overlays,
    scan_stale_spawn_artifacts,
)
from meridian.lib.state import session_store, spawn_store

_EPOCH_NOW = 2_000_000_000.0
_DAY = 24 * 60 * 60


@pytest.fixture(autouse=True)
def _isolate_meridian_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MERIDIAN_HOME", (tmp_path / "user-home").as_posix())


def _set_tree_mtime(path: Path, mtime: float) -> None:
    for current in (path, *path.rglob("*")):
        os.utime(current, (mtime, mtime), follow_symlinks=False)


def _set_path_mtime(path: Path, mtime: float) -> None:
    os.utime(path, (mtime, mtime), follow_symlinks=False)


def _write_payload(path: Path, content: str = "payload") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_scan_orphan_project_dirs_respects_retention_days_semantics(tmp_path: Path) -> None:
    user_home = tmp_path / "user-home"
    projects_root = user_home / "projects"
    stale = projects_root / "stale-uuid"
    fresh = projects_root / "fresh-uuid"
    active = projects_root / "active-uuid"

    _write_payload(stale / "state.txt")
    _write_payload(fresh / "state.txt")
    active.mkdir(parents=True, exist_ok=True)
    spawn_store.start_spawn(
        active,
        chat_id="c1",
        model="gpt-5.4",
        agent="coder",
        harness="codex",
        prompt="active",
    )

    _set_tree_mtime(stale, _EPOCH_NOW - (40 * _DAY))
    _set_tree_mtime(fresh, _EPOCH_NOW - (1 * _DAY))
    _set_tree_mtime(active, _EPOCH_NOW - (40 * _DAY))

    stale_only = scan_orphan_project_dirs(user_home, 30, _EPOCH_NOW)
    aggressive = scan_orphan_project_dirs(user_home, 0, _EPOCH_NOW)
    never = scan_orphan_project_dirs(user_home, -1, _EPOCH_NOW)

    assert [item.uuid for item in stale_only] == ["stale-uuid"]
    assert {item.uuid for item in aggressive} == {"fresh-uuid", "stale-uuid"}
    assert never == []


def test_scan_stale_spawn_artifacts_respects_scope_and_retention_semantics(
    tmp_path: Path,
) -> None:
    user_home = tmp_path / "user-home"
    current_root = user_home / "projects" / "current-uuid"
    other_root = user_home / "projects" / "other-uuid"
    stale_spawn = current_root / "spawns" / "p1"
    active_spawn = current_root / "spawns" / "p2"
    other_spawn = other_root / "spawns" / "p9"

    _write_payload(stale_spawn / "history.jsonl", '{"event":"start"}\n')
    _write_payload(active_spawn / "history.jsonl", '{"event":"start"}\n')
    _write_payload(other_spawn / "history.jsonl", '{"event":"start"}\n')
    _set_tree_mtime(stale_spawn, _EPOCH_NOW - (40 * _DAY))
    _set_tree_mtime(active_spawn, _EPOCH_NOW - (1 * _DAY))
    _set_tree_mtime(other_spawn, _EPOCH_NOW - (40 * _DAY))
    _set_path_mtime(current_root, _EPOCH_NOW - (1 * _DAY))
    _set_path_mtime(other_root, _EPOCH_NOW - (1 * _DAY))

    active_spawn_ids = {"p2"}
    stale_only = scan_stale_spawn_artifacts(current_root, 30, active_spawn_ids, _EPOCH_NOW)
    aggressive = scan_stale_spawn_artifacts(current_root, 0, active_spawn_ids, _EPOCH_NOW)
    never = scan_stale_spawn_artifacts(current_root, -1, active_spawn_ids, _EPOCH_NOW)

    assert [item.spawn_id for item in stale_only] == ["p1"]
    assert [item.spawn_id for item in aggressive] == ["p1"]
    assert never == []
    assert all(item.project_uuid == "current-uuid" for item in stale_only)


def test_scan_stale_claude_overlays_respects_scope_and_retention_semantics(
    tmp_path: Path,
) -> None:
    user_home = tmp_path / "user-home"
    current_root = user_home / "projects" / "current-uuid"
    other_root = user_home / "projects" / "other-uuid"
    stale_overlay = current_root / "claude-config" / "p1"
    active_overlay = current_root / "claude-config" / "p2"
    other_overlay = other_root / "claude-config" / "p9"

    _write_payload(stale_overlay / "projects" / "slug" / "session.jsonl", '{"event":"old"}\n')
    _write_payload(active_overlay / "projects" / "slug" / "session.jsonl", '{"event":"new"}\n')
    _write_payload(other_overlay / "projects" / "slug" / "session.jsonl", '{"event":"other"}\n')
    _set_tree_mtime(stale_overlay, _EPOCH_NOW - (40 * _DAY))
    _set_tree_mtime(active_overlay, _EPOCH_NOW - (1 * _DAY))
    _set_tree_mtime(other_overlay, _EPOCH_NOW - (40 * _DAY))
    _set_path_mtime(current_root, _EPOCH_NOW - (1 * _DAY))
    _set_path_mtime(other_root, _EPOCH_NOW - (1 * _DAY))

    active_spawn_ids = {"p2"}
    stale_only = scan_stale_claude_overlays(current_root, 30, active_spawn_ids, _EPOCH_NOW)
    aggressive = scan_stale_claude_overlays(current_root, 0, active_spawn_ids, _EPOCH_NOW)
    never = scan_stale_claude_overlays(current_root, -1, active_spawn_ids, _EPOCH_NOW)

    assert [item.spawn_id for item in stale_only] == ["p1"]
    assert [item.spawn_id for item in aggressive] == ["p1"]
    assert never == []
    assert all(item.project_uuid == "current-uuid" for item in stale_only)


def test_prune_functions_are_idempotent(tmp_path: Path) -> None:
    user_home = tmp_path / "user-home"
    orphan_dir = user_home / "projects" / "orphan-uuid"
    current_root = user_home / "projects" / "current-uuid"
    stale_spawn = current_root / "spawns" / "p1"

    _write_payload(orphan_dir / "state.txt")
    _write_payload(stale_spawn / "history.jsonl", '{"event":"start"}\n')
    _set_tree_mtime(orphan_dir, _EPOCH_NOW - (40 * _DAY))
    _set_tree_mtime(stale_spawn, _EPOCH_NOW - (40 * _DAY))
    _set_path_mtime(current_root, _EPOCH_NOW - (1 * _DAY))

    orphans = scan_orphan_project_dirs(user_home, 30, _EPOCH_NOW)
    stale = scan_stale_spawn_artifacts(current_root, 30, set(), _EPOCH_NOW)

    assert prune_orphan_project_dirs(orphans) == 1
    assert prune_stale_spawn_artifacts(stale) == 1
    assert not orphan_dir.exists()
    assert not stale_spawn.exists()
    assert prune_orphan_project_dirs(orphans) == 0
    assert prune_stale_spawn_artifacts(stale) == 0


def test_prune_stale_claude_overlays_materializes_transcripts_before_delete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_home = tmp_path / "user-home"
    current_root = user_home / "projects" / "current-uuid"
    stale_overlay = current_root / "claude-config" / "p1"
    canonical_root = user_home / ".claude"
    session_file = stale_overlay / "projects" / "slug" / "session.jsonl"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", canonical_root.as_posix())

    _write_payload(session_file, '{"event":"start"}\n')
    _set_tree_mtime(stale_overlay, _EPOCH_NOW - (40 * _DAY))
    _set_path_mtime(current_root, _EPOCH_NOW - (1 * _DAY))

    stale = scan_stale_claude_overlays(current_root, 30, set(), _EPOCH_NOW)

    assert prune_stale_claude_overlays(stale) == 1
    assert not stale_overlay.exists()
    assert (canonical_root / "projects" / "slug" / "session.jsonl").read_text(
        encoding="utf-8"
    ) == '{"event":"start"}\n'
    assert prune_stale_claude_overlays(stale) == 0


def test_prune_stale_claude_overlays_materializes_auth_state_before_delete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_home = tmp_path / "user-home"
    current_root = user_home / "projects" / "current-uuid"
    stale_overlay = current_root / "claude-config" / "p1"
    canonical_root = user_home / ".claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", canonical_root.as_posix())

    _write_payload(stale_overlay / "projects" / "slug" / "session.jsonl", '{"event":"start"}\n')
    _write_payload(stale_overlay / ".claude.json", '{"auth":"new"}\n')
    _write_payload(stale_overlay / ".credentials.json", '{"token":"new"}\n')
    _write_payload(canonical_root / ".claude.json", '{"auth":"old"}\n')
    _set_tree_mtime(stale_overlay, _EPOCH_NOW - (40 * _DAY))
    _set_path_mtime(current_root, _EPOCH_NOW - (1 * _DAY))

    stale = scan_stale_claude_overlays(current_root, 30, set(), _EPOCH_NOW)

    assert prune_stale_claude_overlays(stale) == 1
    assert not stale_overlay.exists()
    assert (canonical_root / ".claude.json").read_text(encoding="utf-8") == '{"auth":"new"}\n'
    assert (canonical_root / ".credentials.json").read_text(encoding="utf-8") == (
        '{"token":"new"}\n'
    )


def test_prune_stale_claude_overlays_uses_internal_original_root_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_home = tmp_path / "user-home"
    current_root = user_home / "projects" / "current-uuid"
    stale_overlay = current_root / "claude-config" / "p1"
    parent_overlay_root = current_root / "claude-config" / "parent-overlay"
    durable_root = user_home / ".claude-durable"
    session_file = stale_overlay / "projects" / "slug" / "session.jsonl"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", parent_overlay_root.as_posix())
    monkeypatch.setenv(
        MERIDIAN_ORIGINAL_CLAUDE_CONFIG_DIR_ENV,
        durable_root.as_posix(),
    )

    _write_payload(session_file, '{"event":"start"}\n')
    _set_tree_mtime(stale_overlay, _EPOCH_NOW - (40 * _DAY))
    _set_path_mtime(current_root, _EPOCH_NOW - (1 * _DAY))

    stale = scan_stale_claude_overlays(current_root, 30, set(), _EPOCH_NOW)

    assert prune_stale_claude_overlays(stale) == 1
    assert not stale_overlay.exists()
    assert (durable_root / "projects" / "slug" / "session.jsonl").read_text(
        encoding="utf-8"
    ) == '{"event":"start"}\n'
    assert not (parent_overlay_root / "projects" / "slug" / "session.jsonl").exists()


def test_prune_stale_claude_overlays_recovers_durable_root_from_overlay_sidecar(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_home = tmp_path / "user-home"
    current_root = user_home / "projects" / "current-uuid"
    runtime_root = current_root
    stale_overlay_root = runtime_root / "claude-config"
    stale_overlay_root.mkdir(parents=True, exist_ok=True)
    ambient_root = user_home / ".claude-ambient"
    durable_root = user_home / ".claude-durable"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", ambient_root.as_posix())
    monkeypatch.delenv(MERIDIAN_ORIGINAL_CLAUDE_CONFIG_DIR_ENV, raising=False)

    isolated_root, _ = prepare_isolated_claude_config(runtime_root, "p1")
    assert isolated_root is not None
    stale_overlay = isolated_root
    _write_payload(stale_overlay / "projects" / "slug" / "session.jsonl", '{"event":"start"}\n')
    (
        stale_overlay / ".meridian-overlay.json"
    ).write_text(
        '{\n  "v": 1,\n  "materialization_root": "'
        + durable_root.as_posix()
        + '"\n}\n',
        encoding="utf-8",
    )
    _set_tree_mtime(stale_overlay, _EPOCH_NOW - (40 * _DAY))
    _set_path_mtime(current_root, _EPOCH_NOW - (1 * _DAY))

    spawn_store.start_spawn(
        current_root,
        spawn_id="p1",
        chat_id="c1",
        model="gpt-5.4",
        agent="coder",
        harness="claude",
        prompt="seed prompt",
    )
    spawn_store.update_spawn(current_root, "p1", claude_config_dir=stale_overlay.as_posix())
    session_store.start_session(
        current_root,
        harness="claude",
        harness_session_id="sess-1",
        model="gpt-5.4",
        chat_id="c1",
        claude_config_dir=stale_overlay.as_posix(),
    )
    session_store.stop_session(current_root, "c1")

    stale = scan_stale_claude_overlays(current_root, 30, set(), _EPOCH_NOW)

    assert prune_stale_claude_overlays(stale, runtime_root=current_root) == 1
    assert not stale_overlay.exists()
    assert (durable_root / "projects" / "slug" / "session.jsonl").read_text(
        encoding="utf-8"
    ) == '{"event":"start"}\n'
    assert not (ambient_root / "projects" / "slug" / "session.jsonl").exists()
    spawn = spawn_store.get_spawn(current_root, "p1")
    assert spawn is not None
    assert spawn.claude_config_dir == durable_root.as_posix()
    sessions = session_store.get_session_records(current_root, {"c1"})
    assert sessions
    assert sessions[0].claude_config_dir == durable_root.as_posix()


def test_prune_stale_claude_overlays_deletes_even_when_materialization_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_home = tmp_path / "user-home"
    current_root = user_home / "projects" / "current-uuid"
    stale_overlay = current_root / "claude-config" / "p1"

    _write_payload(stale_overlay / "projects" / "slug" / "session.jsonl", '{"event":"start"}\n')
    _set_tree_mtime(stale_overlay, _EPOCH_NOW - (40 * _DAY))
    _set_path_mtime(current_root, _EPOCH_NOW - (1 * _DAY))
    stale = scan_stale_claude_overlays(current_root, 30, set(), _EPOCH_NOW)

    monkeypatch.setattr(
        "meridian.lib.harness.claude_preflight.materialize_overlay_transcripts",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    assert prune_stale_claude_overlays(stale) == 1
    assert not stale_overlay.exists()


def test_prune_stale_claude_overlays_repairs_spawn_and_session_metadata_when_runtime_root_given(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_home = tmp_path / "user-home"
    current_root = user_home / "projects" / "current-uuid"
    stale_overlay = current_root / "claude-config" / "p1"
    durable_root = user_home / ".claude-durable"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", durable_root.as_posix())

    _write_payload(stale_overlay / "projects" / "slug" / "session.jsonl", '{"event":"start"}\n')
    _set_tree_mtime(stale_overlay, _EPOCH_NOW - (40 * _DAY))
    _set_path_mtime(current_root, _EPOCH_NOW - (1 * _DAY))

    spawn_store.start_spawn(
        current_root,
        spawn_id="p1",
        chat_id="c1",
        model="gpt-5.4",
        agent="coder",
        harness="claude",
        prompt="seed prompt",
    )
    spawn_store.update_spawn(current_root, "p1", claude_config_dir=stale_overlay.as_posix())
    session_store.start_session(
        current_root,
        harness="claude",
        harness_session_id="sess-1",
        model="gpt-5.4",
        chat_id="c1",
        claude_config_dir=stale_overlay.as_posix(),
    )
    session_store.stop_session(current_root, "c1")

    stale = scan_stale_claude_overlays(current_root, 30, set(), _EPOCH_NOW)

    assert prune_stale_claude_overlays(stale, runtime_root=current_root) == 1
    assert not stale_overlay.exists()
    spawn = spawn_store.get_spawn(current_root, "p1")
    assert spawn is not None
    assert spawn.claude_config_dir == durable_root.as_posix()
    sessions = session_store.get_session_records(current_root, {"c1"})
    assert sessions
    session = sessions[0]
    assert session.claude_config_dir == durable_root.as_posix()


def test_prune_stale_claude_overlays_skips_metadata_repair_on_partial_materialization_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_home = tmp_path / "user-home"
    current_root = user_home / "projects" / "current-uuid"
    stale_overlay = current_root / "claude-config" / "p1"
    durable_root = user_home / ".claude-durable"

    _write_payload(stale_overlay / "projects" / "slug" / "session.jsonl", '{"event":"start"}\n')
    _set_tree_mtime(stale_overlay, _EPOCH_NOW - (40 * _DAY))
    _set_path_mtime(current_root, _EPOCH_NOW - (1 * _DAY))

    spawn_store.start_spawn(
        current_root,
        spawn_id="p1",
        chat_id="c1",
        model="gpt-5.4",
        agent="coder",
        harness="claude",
        prompt="seed prompt",
    )
    spawn_store.update_spawn(current_root, "p1", claude_config_dir=stale_overlay.as_posix())
    session_store.start_session(
        current_root,
        harness="claude",
        harness_session_id="sess-1",
        model="gpt-5.4",
        chat_id="c1",
        claude_config_dir=stale_overlay.as_posix(),
    )
    session_store.stop_session(current_root, "c1")

    stale = scan_stale_claude_overlays(current_root, 30, set(), _EPOCH_NOW)

    monkeypatch.setattr(
        "meridian.lib.ops.pruning.cleanup_claude_overlay",
        lambda *_args, **_kwargs: ClaudeOverlayCleanupResult(
            materialization_root=durable_root,
            removed=True,
            materialization=ClaudeOverlayMaterializationResult(
                materialization_root=durable_root,
                discovered_transcripts=2,
                copied_transcripts=1,
                failed_transcripts=1,
            ),
        ),
    )

    assert prune_stale_claude_overlays(stale, runtime_root=current_root) == 1
    spawn = spawn_store.get_spawn(current_root, "p1")
    assert spawn is not None
    assert spawn.claude_config_dir == stale_overlay.as_posix()
    sessions = session_store.get_session_records(current_root, {"c1"})
    assert sessions
    assert sessions[0].claude_config_dir == stale_overlay.as_posix()
