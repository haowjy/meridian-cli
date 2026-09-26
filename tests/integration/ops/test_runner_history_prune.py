"""Approved archive rule: bound + exact native resolves + old => drop runner history."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

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
            spawn_id=f"p{n}",
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


_RUNNER_STREAM_NAMES = ("history.jsonl", "last-observed-event.json")


def is_runner_stream_file(name: str) -> bool:
    """True for runner ``history.jsonl``/``last-observed-event.json`` fixtures.

    Production code never reads these; it only stats and unlinks them. The
    blind-mode read trap (``tests/support/runner_history_blind``) enforces
    that for ``history.jsonl``, so ``tree()`` must not call ``read_bytes()``
    on them either.
    """
    parts = tuple(name.split("/"))
    if parts[-1] not in _RUNNER_STREAM_NAMES:
        return False
    if len(parts) == 3 and parts[0] in {"spawns", "artifacts"}:
        return True
    return len(parts) == 4 and parts[0] == "spawns" and parts[2].startswith("attempt-")


def entry_size(entry: tuple[object, ...]) -> int:
    """Byte size from a ``tree()`` entry, whether recorded as bytes or as a stat."""
    content = entry[0]
    return content if isinstance(content, int) else len(content)  # type: ignore[arg-type]


def tree(root: Path, *, authority_only: bool = False) -> dict[str, tuple[object, ...]]:
    """File contents and mtimes; the disposable index and lock files are not authority.

    Runner-stream fixtures (``history.jsonl``, ``last-observed-event.json``)
    are recorded by ``(size, st_ino, st_mtime_ns)`` instead of their bytes:
    production code only stats and unlinks these, so reading their bytes here
    would trip the blind-mode read trap under ``--runner-history=off``. Any
    change to size, inode or mtime still fails the dry-run assertion.
    """
    files: dict[str, tuple[object, ...]] = {}
    for path in root.rglob("*"):
        name = path.relative_to(root).as_posix()
        if not path.is_file() or (authority_only and not_authority(name)):
            continue
        if is_runner_stream_file(name):
            stat = path.stat()
            files[name] = (stat.st_size, stat.st_ino, stat.st_mtime_ns)
        else:
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
        entry_size(before[name]) for name in runner_paths(root, prunable)
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


def test_one_spawn_failure_is_recorded_and_the_pass_continues(
    tmp_path: Path, monkeypatch
) -> None:
    project, root, store = corpus(tmp_path)
    blocked = add_spawn(root, store, 1)
    free = add_spawn(root, store, 2)
    real_unlink = Path.unlink

    def unlink(self: Path, missing_ok: bool = False) -> None:
        if blocked in self.parts:
            raise PermissionError(f"denied: {self.name}")
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", unlink)
    result = prune(project, apply=True)

    assert [row.spawn_id for row in result.pruned] == [free]
    assert len(result.errors) == 1
    assert result.errors[0].startswith(f"{blocked}: denied")
    assert not (root / "spawns" / free / "history.jsonl").exists()


def test_apply_rechecks_native_identity_not_just_file_existence(
    tmp_path: Path, monkeypatch
) -> None:
    from meridian.lib.ops import runner_history_prune

    project, root, store = corpus(tmp_path)
    key = add_spawn(root, store, 1)
    native = store / "00000001-1111-4111-8111-111111111111.jsonl"
    real_mutate = runner_history_prune.mutate_published_spawn_artifact

    def swap_native_then_mutate(*args, **kwargs):
        # Another writer replaces the transcript between planning and the lock.
        native.write_text(json.dumps({"sessionId": "someone-else"}) + "\n")
        return real_mutate(*args, **kwargs)

    monkeypatch.setattr(
        runner_history_prune, "mutate_published_spawn_artifact", swap_native_then_mutate
    )
    result = prune(project, apply=True)

    assert result.pruned == ()
    assert result.errors == (f"{key}: record or native source changed since planning; kept",)
    assert (root / "spawns" / key / "history.jsonl").is_file()


def test_quarantined_rows_are_listed_with_doctor_hint(tmp_path: Path) -> None:
    from meridian.lib.state.history_index import HistoryIndex

    project, root, store = corpus(tmp_path)
    key = add_spawn(root, store, 1)
    HistoryIndex(root).catch_up()  # Dogfood rows predate this build's index.
    state_path = root / "spawns" / key / "state.json"
    state = json.loads(state_path.read_text())
    state.update(entry_chat_id="c1", exit_identity="verified", exit_chat_id="c1")
    state_path.write_text(json.dumps(state))

    result = prune(project)

    assert result.quarantined == (key,)
    hint = f"1 spawns; run `meridian doctor`: {key}"
    assert f"Skipped quarantined: {hint}" in result.format_text()
    with pytest.raises(ValueError, match=f"quarantined: {hint}"):
        session_archive_sync(
            SessionArchiveInput(
                project_root=str(project), eligible=True, destination=str(tmp_path / "zips")
            )
        )


def test_archive_after_prune_captures_native_and_ships_no_runner_members(
    tmp_path: Path,
) -> None:
    import zipfile

    from meridian.lib.config.settings import HistoryArchiveConfig
    from meridian.lib.launch.constants import RETIRED_RUNNER_STREAM_FILENAMES
    from meridian.lib.ops.session_archive import archive_history, materialize_native_history
    from meridian.lib.state.native_snapshot import NATIVE_SNAPSHOT_FILENAME

    project, root, store = corpus(tmp_path)
    key = add_spawn(root, store, 1)
    assert [row.spawn_id for row in prune(project, apply=True).pruned] == [key]

    materialize_native_history(project, root, key)
    assert (root / "spawns" / key / NATIVE_SNAPSHOT_FILENAME).is_file()
    out = archive_history(
        root,
        destination=tmp_path / "zips",
        refs=(key,),
        eligible=False,
        apply=True,
        after_days=0,
        policy=HistoryArchiveConfig(),
        project_root=project,
    )

    assert out.errors == ()
    assert len(out.selected) == 1
    assert out.reclaimed == out.selected
    (archive,) = out.archives
    members = {Path(name).name for name in zipfile.ZipFile(archive).namelist()}
    assert NATIVE_SNAPSHOT_FILENAME in members
    assert not members & set(RETIRED_RUNNER_STREAM_FILENAMES)
