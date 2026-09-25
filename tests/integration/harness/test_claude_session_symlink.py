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
