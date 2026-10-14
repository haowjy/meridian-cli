"""An older live run must not write or read a successor's mutable scope/report."""

from pathlib import Path

from meridian.lib.core.refs import parse_ref
from meridian.lib.ops.context import (
    TaskDirClearInput,
    TaskDirSetInput,
    task_dir_clear_sync,
    task_dir_set_sync,
)
from meridian.lib.ops.report import ReportShowInput, report_show_sync
from meridian.lib.state import spawn_store
from meridian.lib.state.spawn_scope import read_spawn_scope, write_spawn_scope_task_dir


def test_ambient_self_targets_exact_run_and_old_launcher_falls_back(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    (project / "meridian.toml").write_text('[project]\nid="run-scope"\n')
    home = tmp_path / "home"
    root = home / "projects" / "run-scope"
    monkeypatch.chdir(project)
    monkeypatch.setenv("MERIDIAN_HOME", str(home))
    monkeypatch.setenv("MERIDIAN_PROJECT_DIR", str(project))
    monkeypatch.setenv("MERIDIAN_SPAWN_ID", "p1")
    monkeypatch.setenv("MERIDIAN_RUN_ID", "p1.2")
    for value in ("p1", "p1.2", "p1.3"):
        run = spawn_store.start_spawn(
            root, spawn_id=value, chat_id="c1", model="test", agent="test",
            harness="codex", prompt="fixture",
        )
        (root / "spawns" / run / "report.md").write_text(value)
        write_spawn_scope_task_dir(project, parse_ref(value, row_key=True), project)

    selected = tmp_path / "selected"
    selected.mkdir()
    task_dir_set_sync(TaskDirSetInput(path=str(selected)))
    assert read_spawn_scope(project, parse_ref("p1.2", row_key=True)).task_dir == selected
    for sibling in ("p1", "p1.3"):
        assert read_spawn_scope(project, parse_ref(sibling, row_key=True)).task_dir == project
    assert report_show_sync(ReportShowInput()).report == "p1.2"
    task_dir_clear_sync(TaskDirClearInput())
    assert read_spawn_scope(project, parse_ref("p1.2", row_key=True)).task_dir_cleared

    monkeypatch.delenv("MERIDIAN_RUN_ID")
    assert report_show_sync(ReportShowInput()).report == "p1"
    assert read_spawn_scope(project, parse_ref("p1", row_key=True)).task_dir == Path(project)


def test_child_history_edge_names_launching_parent_run_not_latest(tmp_path, monkeypatch):
    from meridian.lib.core.lifecycle import SpawnLifecycleService
    from meridian.lib.launch.request import SpawnRequest
    from meridian.lib.ops.spawn.execute_init import build_spawn_reservation
    from meridian.lib.ops.spawn.models import SpawnCreateInput

    for key in ("p1", "p1.2", "p1.3"):
        spawn_store.start_spawn(
            tmp_path, spawn_id=key, chat_id="c1", model="test", agent="test",
            harness="codex", prompt="fixture",
        )
    monkeypatch.setenv("MERIDIAN_SPAWN_ID", "p1")
    monkeypatch.setenv("MERIDIAN_RUN_ID", "p1.2")
    reservation = build_spawn_reservation(
        payload=SpawnCreateInput(prompt="fixture"),
        request=SpawnRequest(prompt="fixture", model="test", agent="test", harness="codex"),
    )
    child = SpawnLifecycleService(tmp_path).reserve(reservation)
    parent = spawn_store.get_spawn(tmp_path, parse_ref("p1.2", row_key=True))
    row = spawn_store.get_spawn(tmp_path, parse_ref(child, row_key=True))
    assert parent is not None and row is not None
    assert row.parent_id == "p1"
    assert row.parent_history_id == parent.history_id
