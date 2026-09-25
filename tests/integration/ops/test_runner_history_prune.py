"""Approved archive rule: bound + exact native resolves + old => drop runner history."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from meridian.lib.ops.session_archive import (
    SessionArchiveInput,
    session_archive_sync,
    session_stop_maintenance,
)
from meridian.lib.state import session_store, spawn_store
from meridian.lib.state.spawn.model import RunBoundaryOutcome

OLD = (datetime.now(UTC) - timedelta(days=30)).isoformat()
RUNNER_FILES = (
    "spawns/{key}/history.jsonl",
    "spawns/{key}/last-observed-event.json",
    "spawns/{key}/.history.jsonl.deadbeef.tmp",
    "spawns/{key}/attempt-1/history.jsonl",
    "spawns/{key}/attempt-1/last-observed-event.json",
    "artifacts/{key}/history.jsonl",
)
KEPT_FILES = (
    "spawns/{key}/report.md",
    "spawns/{key}/pi-lifecycle.json",
    "spawns/{key}/stderr.log",
    "spawns/{key}/attempt-1/report.md",
    "artifacts/{key}/report.md",
)


def corpus(tmp_path: Path) -> tuple[Path, Path, Path]:
    project = tmp_path / "project"
    project.mkdir()
    (project / "meridian.toml").write_text('[project]\nid="prune-runner"\n')
    from meridian.lib.state.user_paths import get_project_home

    root = get_project_home("prune-runner")
    root.mkdir(parents=True)
    store = tmp_path / "native"
    store.mkdir()
    return project, root, store


def add_spawn(
    root: Path,
    store: Path,
    n: int,
    *,
    bound: bool = True,
    finished_at: str | None = OLD,
    boundary: RunBoundaryOutcome | None = None,
) -> str:
    sid = f"{n:08d}-1111-4111-8111-111111111111"
    chat = f"c{n}"
    if bound:
        (store / f"{sid}.jsonl").write_text(
            json.dumps({"sessionId": sid})
            + "\n"
            + json.dumps({"type": "assistant", "message": {"content": f"needle {n}"}})
            + "\n"
        )
        session_store.start_session(
            root,
            harness="claude",
            harness_session_id=sid,
            native_store=str(store),
            model="test",
            chat_id=chat,
        )
        session_store.stop_session(root, chat)
    key = spawn_store.start_spawn(
        root,
        spawn_id=f"p{n}",
        chat_id=chat,
        harness="claude",
        model="test",
        agent="a",
        prompt="question",
        harness_session_id=sid if bound else None,
    )
    if boundary is not None:
        spawn_store.update_spawn(root, key, run_boundary=boundary)
    if finished_at is not None:
        spawn_store.finalize_spawn(
            root, key, "succeeded", 0, origin="runner", finished_at=finished_at
        )
    for template in (*RUNNER_FILES, *KEPT_FILES):
        path = root / template.format(key=key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{template} runner event\n")
    return key


def not_authority(name: str) -> bool:
    return name.startswith(("history-index/", "locks/")) or name.endswith(".lock")


def tree(root: Path, *, authority_only: bool = False) -> dict[str, tuple[bytes, int]]:
    """File bytes and mtimes; the disposable index and lock files are not authority."""
    files: dict[str, tuple[bytes, int]] = {}
    for path in root.rglob("*"):
        name = path.relative_to(root).as_posix()
        if path.is_file() and not (authority_only and not_authority(name)):
            files[name] = (path.read_bytes(), path.stat().st_mtime_ns)
    return files


def prune(project: Path, *, apply: bool = False, after_days: int | None = None):
    result = session_archive_sync(
        SessionArchiveInput(
            project_root=str(project),
            prune_runner_history=True,
            apply=apply,
            after_days=after_days,
        )
    )
    assert result.runner_history is not None
    return result.runner_history


def runner_paths(root: Path, key: str) -> set[str]:
    return {template.format(key=key) for template in RUNNER_FILES}


def test_qualification_matrix_and_apply_removes_only_runner_files(tmp_path: Path) -> None:
    project, root, store = corpus(tmp_path)
    prunable = add_spawn(root, store, 1)
    recent = add_spawn(root, store, 2, finished_at=datetime.now(UTC).isoformat())
    running = add_spawn(root, store, 3, finished_at=None)
    unbound = add_spawn(root, store, 4, bound=False)
    missing = add_spawn(root, store, 5)
    (store / "00000005-1111-4111-8111-111111111111.jsonl").unlink()
    mismatch = add_spawn(root, store, 6)
    (store / "00000006-1111-4111-8111-111111111111.jsonl").write_text(
        json.dumps({"sessionId": "someone-else"}) + "\n"
    )
    unresolved = add_spawn(root, store, 7, boundary=RunBoundaryOutcome(status="unresolved"))

    from meridian.lib.ops.runtime import resolve_roots_for_read

    resolve_roots_for_read(str(project))  # One-time startup import, not the dry run.
    before = tree(root)
    planned = prune(project)
    assert tree(root) == before, "dry-run must not change bytes or mtimes"
    assert [row.spawn_id for row in planned.pruned] == [prunable]
    assert planned.pruned[0].bytes == sum(
        len(before[name][0]) for name in runner_paths(root, prunable)
    )
    assert {reason: rows for reason, rows in planned.skipped.items()} == {
        "recent": (recent,),
        "running": (running,),
        "unbound": (unbound,),
        "missing": (missing,),
        "error": (mismatch,),
        "exit_unresolved": (unresolved,),
    }

    applied = prune(project, apply=True)
    assert [row.spawn_id for row in applied.pruned] == [prunable]
    after = tree(root, authority_only=True)
    removed = runner_paths(root, prunable)
    assert removed <= set(before)
    assert after == {
        name: value
        for name, value in before.items()
        if name not in removed and not not_authority(name)
    }
    for template in KEPT_FILES:
        assert (root / template.format(key=prunable)).is_file()

    rerun = prune(project, apply=True)
    assert rerun.pruned == ()
    assert tree(root, authority_only=True) == after


def test_after_days_is_measured_from_terminal_time(tmp_path: Path) -> None:
    project, root, store = corpus(tmp_path)
    key = add_spawn(root, store, 1)
    assert prune(project, after_days=60).skipped == {"recent": (key,)}
    assert [row.spawn_id for row in prune(project, after_days=29).pruned] == [key]


def test_reads_still_work_after_prune(tmp_path: Path) -> None:
    from meridian.lib.ops.session_search import SessionSearchInput, session_search_sync
    from meridian.lib.ops.session_transcript import read_session_transcript
    from meridian.lib.ops.spawn.api import spawn_show_sync
    from meridian.lib.ops.spawn.models import SpawnShowInput

    project, root, store = corpus(tmp_path)
    key = add_spawn(root, store, 1)

    def reads() -> tuple[object, ...]:
        log = read_session_transcript(ref=key, file_path=None, project_root=str(project))
        show = spawn_show_sync(SpawnShowInput(spawn_id=key, project_root=str(project)))
        search = session_search_sync(SessionSearchInput(query="needle", project_root=str(project)))
        return (
            [repr(entry) for entry in log.all_entries],
            show.model_dump(),
            [match.model_dump() for match in search.matches],
        )

    before = reads()
    assert before[2], "fixture must be searchable"
    assert [row.spawn_id for row in prune(project, apply=True).pruned] == [key]
    assert not (root / "spawns" / key / "history.jsonl").exists()
    assert reads() == before


def test_automatic_maintenance_never_prunes(tmp_path: Path, monkeypatch) -> None:
    project, root, store = corpus(tmp_path)
    key = add_spawn(root, store, 1)
    monkeypatch.setenv("MERIDIAN_HISTORY_ARCHIVE_AUTOMATIC", "true")
    monkeypatch.setenv("MERIDIAN_HISTORY_ARCHIVE_DESTINATION", str(tmp_path / "zips"))
    monkeypatch.setenv("MERIDIAN_HISTORY_ARCHIVE_AFTER_DAYS", "365")
    session_stop_maintenance(project, key)
    for name in runner_paths(root, key):
        assert (root / name).is_file()


def test_unreleased_live_scope_skips_terminal_spawn(tmp_path: Path) -> None:
    import os

    import psutil

    from meridian.lib.core.types import SpawnId
    from meridian.lib.platform.process_scope.base import ProcessScopeSnapshot
    from meridian.lib.state.process_scope_projection import mark_scope_released, record_scope

    project, root, store = corpus(tmp_path)
    key = add_spawn(root, store, 1, finished_at=None)
    scope = ProcessScopeSnapshot(
        scope_id="backend",
        owner_policy="session_owned",
        owner_id="native",
        role="harness_backend",
        containment="pid_tree_fallback",
        root_pid=os.getpid(),
        root_created_at_epoch=psutil.Process().create_time(),
        pgid=None,
        job_name=None,
        degraded_reason=None,
    )
    record_scope(root, SpawnId(key), scope)
    spawn_store.finalize_spawn(root, key, "succeeded", 0, origin="runner", finished_at=OLD)
    assert prune(project, apply=True).skipped == {"live_scope": (key,)}
    assert (root / "spawns" / key / "history.jsonl").is_file()
    mark_scope_released(root, SpawnId(key), scope.release_id)
    assert [row.spawn_id for row in prune(project).pruned] == [key]
