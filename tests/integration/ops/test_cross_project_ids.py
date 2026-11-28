"""A claimed ID finds its project from another one; nothing is recorded for the caller there."""

from pathlib import Path

import pytest

import meridian.lib.ops.spawn.wait as spawn_wait
from meridian.lib.context.runtime import RuntimeContext
from meridian.lib.core.refs import RefAmbiguous, RefNotFound, project_named_scope
from meridian.lib.ops.locate import SpawnHit, locate
from meridian.lib.ops.spawn.models import SpawnWaitInput
from meridian.lib.state import spawn_store
from meridian.lib.state.machine_ids import ids_dir
from meridian.lib.state.paths import resolve_project_runtime_root_for_write
from tests.support.launch_paths import launch_paths


def _project(tmp_path: Path, name: str) -> tuple[Path, Path]:
    project_root = tmp_path / name
    project_root.mkdir()
    runtime_root = resolve_project_runtime_root_for_write(project_root)
    runtime_root.mkdir(parents=True, exist_ok=True)
    return project_root, runtime_root


def _start(project_root: Path, runtime_root: Path, **kwargs: str) -> str:
    return str(
        spawn_store.start_spawn(
            runtime_root,
            chat_id=None,
            model="test",
            agent="coder",
            harness="pi",
            prompt="work",
            control_root=project_root.as_posix(),
            **kwargs,
        ).spawn_id
    )


def test_claimed_id_resolves_from_another_project_with_its_project_root(tmp_path: Path) -> None:
    here, _ = _project(tmp_path, "here")
    there, there_root = _project(tmp_path, "there")
    claimed = _start(there, there_root)
    never_issued = "p99999"
    # Without the test home's planted p0 claim, p0 is below every claim: issued before them.
    (ids_dir() / "p" / "0" / "p0").unlink()
    legacy = _start(there, there_root, spawn_id="p0")

    hit = locate(here, claimed)

    assert isinstance(hit, SpawnHit)
    assert (hit.runtime_root, hit.project_root) == (there_root, there)
    with pytest.raises(RefNotFound) as exc:
        locate(here, never_issued)
    assert "issued before" not in str(exc.value)
    with pytest.raises(RefNotFound, match="issued before machine-wide IDs"):
        locate(here, legacy)


def test_id_held_by_both_projects_is_refused_unless_a_project_is_named(tmp_path: Path) -> None:
    here, here_root = _project(tmp_path, "here")
    there, there_root = _project(tmp_path, "there")
    claimed = _start(there, there_root)
    _start(here, here_root, spawn_id=claimed)

    with pytest.raises(RefAmbiguous):
        locate(here, claimed)
    with project_named_scope(True):
        hit = locate(here, claimed)
    assert isinstance(hit, SpawnHit)
    assert hit.runtime_root == here_root


def test_wait_on_another_projects_spawn_records_nothing_for_the_caller(tmp_path: Path) -> None:
    here, here_root = _project(tmp_path, "here")
    there, there_root = _project(tmp_path, "there")
    target = _start(there, there_root)
    spawn_store.finalize_spawn(there_root, target, "succeeded", 0, origin="runner")
    launch_paths(there_root, target).report.write_text("done\n", encoding="utf-8")
    caller = _start(here, here_root)

    result = spawn_wait.spawn_wait_sync(
        SpawnWaitInput(project_root=str(here), spawn_ids=(target,)),
        ctx=RuntimeContext(spawn_id=caller, runtime_root=here_root),
    )

    assert [spawn.spawn_id for spawn in result.spawns] == [target]
    assert not list(tmp_path.rglob("observed-launches.json"))
