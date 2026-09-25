"""Exact Claude source seeding is idempotent and rejects path traversal."""
from pathlib import Path

import pytest

from meridian.lib.core.native_identity import NativeSessionUnavailable
from meridian.lib.harness.claude_preflight import ensure_claude_session_accessible
from meridian.lib.harness.claude_sessions import project_slug


@pytest.mark.parametrize("same_config", [False, True])
def test_exact_source_seed_is_idempotent(tmp_path: Path, same_config: bool) -> None:
    config = tmp_path / "config"
    source = config / "projects" / "source"
    source.mkdir(parents=True)
    native = source / "session-1.jsonl"
    native.write_text('{"sessionId":"session-1"}\n')
    target_config = config if same_config else tmp_path / "other-config"
    target_cwd = tmp_path / "child"
    for _ in range(2):
        ensure_claude_session_accessible(
            "session-1", target_cwd,
            source_native_store=source, target_config_root=target_config,
        )
    target = target_config / "projects" / project_slug(target_cwd) / native.name
    assert target.read_text() == '{"sessionId":"session-1"}\n'
    assert target.is_symlink() == same_config


@pytest.mark.parametrize("session_id", ["../escape", "a/b", ".."])
def test_source_id_cannot_escape_store(tmp_path: Path, session_id: str) -> None:
    with pytest.raises(NativeSessionUnavailable):
        ensure_claude_session_accessible(
            session_id, tmp_path, source_native_store=tmp_path,
        )


@pytest.mark.parametrize("failure", ["symlink", "replace"])
def test_symlink_publication_failure_preserves_previous_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    import os

    config = tmp_path / "config"
    source = config / "projects" / "source"
    source.mkdir(parents=True)
    (source / "session-1.jsonl").write_text('{"sessionId":"session-1"}\n')
    child = tmp_path / "child"
    target = config / "projects" / project_slug(child) / "session-1.jsonl"
    target.parent.mkdir(parents=True)
    target.write_text("previous target\n")

    def fail(*args: object, **kwargs: object) -> None:
        raise OSError("publication failed")

    if failure == "symlink":
        monkeypatch.setattr(Path, "symlink_to", fail)
    else:
        monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(OSError, match="publication failed"):
        ensure_claude_session_accessible(
            "session-1", child, source_native_store=source, target_config_root=config,
        )
    assert target.read_text() == "previous target\n"
    assert list(target.parent.iterdir()) == [target]
