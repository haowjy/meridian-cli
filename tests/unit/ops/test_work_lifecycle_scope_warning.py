from __future__ import annotations

from pathlib import Path

from meridian.lib.ops.runtime import resolve_roots
from meridian.lib.ops.work_attachment import set_session_work_attachment
from meridian.lib.ops.work_lifecycle import (
    WorkStartInput,
    WorkSwitchInput,
    format_leave_scope_warning,
    work_scope_label,
    work_start_sync,
    work_switch_sync,
)
from meridian.lib.state import session_store, work_store
from meridian.lib.state.paths import resolve_ambient_work_dir
from meridian.lib.state.work_scope import SCOPE_HANDOFFS_DIRNAME, SCOPE_PROMPTS_DIRNAME, WorkScope


def _setup_project(tmp_path: Path) -> tuple[Path, Path, Path]:
    project_root = tmp_path / "repo"
    project_root.mkdir(parents=True, exist_ok=True)
    (project_root / "mars.toml").write_text("", encoding="utf-8")
    id_path = project_root / ".meridian" / "id"
    id_path.parent.mkdir(parents=True, exist_ok=True)
    id_path.write_text("proj-scope-warn", encoding="utf-8")
    roots = resolve_roots(project_root.as_posix())
    return project_root, roots.project_state_dir, roots.runtime_root


def test_work_start_warns_when_leaving_scope_with_handoffs(tmp_path: Path, monkeypatch) -> None:
    project_root, _project_state_dir, runtime_root = _setup_project(tmp_path)
    monkeypatch.setenv("MERIDIAN_PROJECT_DIR", project_root.as_posix())
    monkeypatch.setenv("MERIDIAN_SPAWN_ID", "p1")
    monkeypatch.delenv("MERIDIAN_ACTIVE_WORK_ID", raising=False)
    session_store.start_session(
        runtime_root,
        harness="codex",
        harness_session_id="session-chat-1",
        model="gpt-5.4",
        chat_id="chat-1",
    )

    ambient = resolve_ambient_work_dir(project_root, "p1")
    handoffs = ambient / SCOPE_HANDOFFS_DIRNAME
    handoffs.mkdir(parents=True)
    (handoffs / "ctx.md").write_text("keep me", encoding="utf-8")

    output = work_start_sync(
        WorkStartInput(
            label="next-scope",
            project_root=project_root.as_posix(),
            chat_id="chat-1",
        )
    )

    assert output.warning is not None
    assert "1 artifact" in output.warning
    assert "spawn-local work area (p1)" in output.warning
    assert "next-scope" in output.warning
    assert session_store.get_session_active_work_id(runtime_root, "chat-1") == "next-scope"


def test_work_start_silent_when_leaving_prompts_only_scope(tmp_path: Path, monkeypatch) -> None:
    project_root, _project_state_dir, _runtime_root = _setup_project(tmp_path)
    monkeypatch.setenv("MERIDIAN_PROJECT_DIR", project_root.as_posix())
    monkeypatch.setenv("MERIDIAN_SPAWN_ID", "p2")
    monkeypatch.delenv("MERIDIAN_ACTIVE_WORK_ID", raising=False)

    prompts = resolve_ambient_work_dir(project_root, "p2") / SCOPE_PROMPTS_DIRNAME
    prompts.mkdir(parents=True)
    (prompts / "launch.md").write_text("disposable", encoding="utf-8")

    output = work_start_sync(
        WorkStartInput(
            label="clean-switch",
            project_root=project_root.as_posix(),
            chat_id="chat-2",
        )
    )

    leave_warnings = [
        part
        for part in (output.warning or "").split("\n")
        if "won't follow you" in part
    ]
    assert leave_warnings == []


def test_work_switch_warns_when_leaving_named_scope_with_artifacts(tmp_path: Path) -> None:
    project_root, project_state_dir, runtime_root = _setup_project(tmp_path)
    first = work_store.create_work_item(project_state_dir, "scope-a", "", None)
    second = work_store.create_work_item(project_state_dir, "scope-b", "", None)
    scope_a = work_store.work_scratch_dir(project_state_dir, first.name)
    (scope_a / "notes.md").write_text("artifact", encoding="utf-8")
    session_store.start_session(
        runtime_root,
        harness="codex",
        harness_session_id="session-chat-3",
        model="gpt-5.4",
        chat_id="chat-3",
    )
    set_session_work_attachment(runtime_root, chat_id="chat-3", work_id=first.name)

    output = work_switch_sync(
        WorkSwitchInput(
            work_id=second.name,
            chat_id="chat-3",
            project_root=project_root.as_posix(),
        )
    )

    assert output.warning is not None
    assert "1 artifact" in output.warning
    assert "work item 'scope-a'" in output.warning
    assert "scope-b" in output.warning


def test_work_scope_label_differs_by_kind() -> None:
    durable = WorkScope(kind="work_item", identifier="feature-a", root=Path("/w/feature-a"))
    ephemeral = WorkScope(kind="ambient_spawn", identifier="p9", root=Path("/w/p9"))

    assert work_scope_label(durable) == "work item 'feature-a'"
    assert work_scope_label(ephemeral) == "spawn-local work area (p9)"


def test_format_leave_scope_warning_uses_kind_specific_label(tmp_path: Path) -> None:
    durable_dir = tmp_path / "scope-a"
    durable_dir.mkdir()
    durable = WorkScope(kind="work_item", identifier="scope-a", root=durable_dir)
    ephemeral_dir = tmp_path / "p1"
    ephemeral_dir.mkdir()
    ephemeral = WorkScope(
        kind="ambient_spawn",
        identifier="p1",
        root=ephemeral_dir,
    )

    assert format_leave_scope_warning(durable, "scope-b") is None

    (durable_dir / "notes.md").write_text("x", encoding="utf-8")

    warning = format_leave_scope_warning(durable, "scope-b")
    assert warning is not None
    assert "work item 'scope-a'" in warning
    assert "scope-b" in warning

    (ephemeral_dir / "scratch.md").write_text("x", encoding="utf-8")

    ambient_warning = format_leave_scope_warning(ephemeral, "next-scope")
    assert ambient_warning is not None
    assert "spawn-local work area (p1)" in ambient_warning
    assert "next-scope" in ambient_warning
