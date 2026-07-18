from pathlib import Path

import pytest

from meridian.lib.ops.spawn.context_ref import render_context_refs, resolve_context_ref
from meridian.lib.state import session_store


def test_resolve_context_ref_rejects_filesystem_path(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="--from does not accept filesystem paths"):
        resolve_context_ref(tmp_path, "./notes/context.md")

    with pytest.raises(ValueError, match="use --file/-f instead"):
        resolve_context_ref(tmp_path, "/tmp/report.md")


def test_resolve_context_ref_still_accepts_spawn_and_session_refs(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Spawn 'p999' not found"):
        resolve_context_ref(tmp_path, "p999")

    with pytest.raises(ValueError, match="No primary spawn found"):
        resolve_context_ref(tmp_path, "c999")


def test_resolve_context_ref_accepts_primary_session_without_spawn_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root = tmp_path / ".meridian"
    runtime_root.mkdir()

    def resolve_runtime_root(_project_root: Path) -> Path:
        return runtime_root

    monkeypatch.setattr(
        "meridian.lib.ops.spawn.context_ref.resolve_runtime_root_for_read",
        resolve_runtime_root,
    )
    chat_id = session_store.start_session(
        runtime_root,
        harness="codex",
        harness_session_id="thread-primary",
        model="gpt-5.4",
        kind="primary",
    )

    try:
        resolved = resolve_context_ref(tmp_path, chat_id)
    finally:
        session_store.stop_session(runtime_root, chat_id)

    assert resolved.ref_kind == "session"
    assert resolved.chat_id == chat_id
    assert resolved.harness_session_id == "thread-primary"
    assert resolved.primary_spawn_id is None

    rendered = render_context_refs((resolved,))
    assert f"meridian session log {chat_id}" in rendered
    assert "meridian spawn show" not in rendered
