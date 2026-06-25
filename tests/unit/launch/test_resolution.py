from pathlib import Path

import pytest

from meridian.lib.launch.resolution import resolve_launch_inputs
from meridian.lib.ops.spawn import context_ref
from meridian.lib.state import work_store


def _stub_context_from_work(
    monkeypatch: pytest.MonkeyPatch,
    work_id: str | None,
) -> None:
    def fake_resolve_context_ref(project_root: Path, ref: str) -> context_ref.ContextRef:
        _ = (project_root, ref)
        return context_ref.SpawnContextRef(
            spawn_id="p123",
            status="succeeded",
            agent="coder",
            desc="prior task",
            model="gpt-5.5",
            harness="codex",
            work_id=work_id,
        )

    monkeypatch.setattr(context_ref, "resolve_context_ref", fake_resolve_context_ref)


def test_resolve_launch_inputs_inherits_context_work_as_last_resort(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_context_from_work(monkeypatch, "from-work")

    resolution = resolve_launch_inputs(
        authority_root=tmp_path,
        project_state_dir=tmp_path / ".meridian",
        context_from=("p123",),
        reference_files=(),
    )

    assert resolution.effective_work_id == "from-work"
    assert resolution.context_work_id == "from-work"
    assert resolution.directory_context.work_item == "from-work"
    assert resolution.directory_context.task_cwd_source == "ambient-work-authority-root"
    assert resolution.request_updates["work_id_hint"] == "from-work"


def test_resolve_launch_inputs_keeps_explicit_and_ambient_precedence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_context_from_work(monkeypatch, "from-work")

    explicit = resolve_launch_inputs(
        authority_root=tmp_path,
        project_state_dir=tmp_path / ".meridian",
        context_from=("p123",),
        reference_files=(),
        explicit_work_id="explicit-work",
        ambient_work_id="ambient-work",
    )
    ambient = resolve_launch_inputs(
        authority_root=tmp_path,
        project_state_dir=tmp_path / ".meridian",
        context_from=("p123",),
        reference_files=(),
        ambient_work_id="ambient-work",
    )

    assert explicit.effective_work_id == "explicit-work"
    assert explicit.context_work_id is None
    assert explicit.directory_context.task_cwd_source == "explicit-work-authority-root"
    assert ambient.effective_work_id == "ambient-work"
    assert ambient.context_work_id is None
    assert ambient.directory_context.task_cwd_source == "ambient-work-authority-root"


def test_resolve_launch_inputs_resolves_references_from_selected_task_dir(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    task_dir = tmp_path / "task"
    project_root.mkdir()
    task_dir.mkdir()
    (project_root / "task-only.txt").write_text("project shadow", encoding="utf-8")
    task_file = task_dir / "task-only.txt"
    task_file.write_text("task marker", encoding="utf-8")
    project_state_dir = project_root / ".meridian"
    work_store.ensure_work_item_metadata(project_state_dir, "task-work")
    work_store.update_work_item_task_dir(
        project_state_dir,
        "task-work",
        task_dir=task_dir.as_posix(),
    )

    resolution = resolve_launch_inputs(
        authority_root=project_root,
        project_state_dir=project_state_dir,
        context_from=(),
        reference_files=("task-only.txt",),
        explicit_work_id="task-work",
    )

    assert resolution.directory_context.reference_anchor == task_dir.resolve()
    assert resolution.reference_files == (task_file.resolve(),)
    assert resolution.runtime_updates["requested_task_cwd"] == task_dir.resolve().as_posix()


def test_resolve_launch_inputs_passes_inherited_task_dir(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    inherited = tmp_path / "inherited"
    inherited.mkdir()
    project_state_dir = project_root / ".meridian"
    project_state_dir.mkdir(parents=True)

    resolution = resolve_launch_inputs(
        authority_root=project_root,
        project_state_dir=project_state_dir,
        context_from=(),
        reference_files=(),
        inherited_task_dir=inherited.as_posix(),
    )

    assert resolution.directory_context.logical_task_cwd == inherited.resolve()
    assert resolution.directory_context.task_cwd_source == "inherited-task-dir"
