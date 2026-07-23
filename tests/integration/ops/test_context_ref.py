from pathlib import Path

from meridian.lib.ops.spawn.context_ref import resolve_context_ref
from meridian.lib.state import session_store, spawn_store
from meridian.lib.state.paths import resolve_project_runtime_root_for_write


def test_from_harness_session_uuid_resolves_primary_spawn(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    (project_root / "mars.toml").write_text(
        '[settings]\ntargets = [".claude", ".codex", ".opencode"]\n',
        encoding="utf-8",
    )
    runtime_root = resolve_project_runtime_root_for_write(project_root)
    runtime_root.mkdir(parents=True, exist_ok=True)

    harness_session_id = "11111111-1111-4111-8111-111111111111"
    chat_id = session_store.start_session(
        runtime_root,
        harness="codex",
        harness_session_id=harness_session_id,
        model="gpt-5.4",
        kind="primary",
    )
    spawn_store.start_spawn(
        runtime_root,
        spawn_id="p1",
        chat_id=chat_id,
        model="gpt-5.4",
        agent="coder",
        harness="codex",
        kind="primary",
        prompt="original prompt",
        harness_session_id=harness_session_id,
        task_cwd=project_root.as_posix(),
    )

    resolved = resolve_context_ref(project_root, harness_session_id)

    assert resolved.ref_kind == "session"
    assert resolved.chat_id == chat_id
    assert resolved.primary_spawn_id == "p1"
    assert resolved.harness_session_id == harness_session_id
