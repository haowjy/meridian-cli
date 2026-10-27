from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from meridian.lib.core.context import RuntimeContext
from meridian.lib.core.types import HarnessId, SpawnId
from meridian.lib.launch.cwd import inheritable_task_dir
from meridian.lib.launch.request import SpawnRequest
from meridian.lib.ops.spawn.execute import _resolve_execution_contract
from meridian.lib.ops.spawn.models import SpawnCreateInput
from meridian.lib.state import work_repository
from meridian.lib.state.paths import resolve_project_paths

pytestmark = pytest.mark.slow


def _project(tmp_path: Path) -> Path:
    project_root = tmp_path / "repo"
    project_root.mkdir(parents=True)
    (project_root / ".meridian").mkdir(parents=True)
    return project_root


def test_inheritable_task_dir_passes_on_only_the_launchers_pin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = _project(tmp_path)
    state_dir = resolve_project_paths(project_root).root_dir
    work_task_dir = tmp_path / "work-task"
    work_task_dir.mkdir(parents=True)
    inherited = tmp_path / "inherited"
    inherited.mkdir(parents=True)
    item = work_repository.create_work_item(state_dir, "feature", "", None)
    work_repository.update_work_item_task_dir(
        state_dir,
        item.name,
        task_dir=work_task_dir.as_posix(),
    )
    # Inside a managed run `MERIDIAN_TASK_DIR` is the launcher's own link, never a pin.
    monkeypatch.setenv("MERIDIAN_TASK_DIR", inherited.as_posix())

    assert inheritable_task_dir(project_root, "p-parent") is None
    # Outside one, a value the user exported is an explicit choice.
    assert inheritable_task_dir(project_root, None) == inherited.resolve()


def test_execute_contract_does_not_shadow_ambient_work_item_task_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = _project(tmp_path)
    state_dir = resolve_project_paths(project_root).root_dir
    work_task_dir = tmp_path / "work-task"
    work_task_dir.mkdir(parents=True)
    item = work_repository.create_work_item(state_dir, "feature", "", None)
    work_repository.update_work_item_task_dir(
        state_dir,
        item.name,
        task_dir=work_task_dir.as_posix(),
    )
    monkeypatch.delenv("MERIDIAN_TASK_DIR", raising=False)

    contract = _resolve_execution_contract(
        request=SpawnRequest(
            prompt="task",
            model="gpt-5.4",
            harness=HarnessId.CODEX.value,
            authority_root=project_root.as_posix(),
            task_cwd="",
        ),
        project_paths=SimpleNamespace(project_root=project_root),
        payload=SpawnCreateInput(
            prompt="task",
            model="gpt-5.4",
            harness="codex",
            project_root=project_root.as_posix(),
        ),
        ctx=RuntimeContext(spawn_id=SpawnId("p-parent"), work_id=item.name),
    )

    assert contract.execution_cwd == work_task_dir.resolve()
