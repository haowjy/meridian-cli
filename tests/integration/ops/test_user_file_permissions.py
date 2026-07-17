"""Permission-policy regressions for user-owned project files."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from meridian.lib.ops.init_ops import maybe_scaffold_claude_agent_copy
from meridian.lib.platform import IS_WINDOWS
from meridian.lib.state import work_repository


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@pytest.mark.skipif(IS_WINDOWS, reason="POSIX file modes are not enforced on Windows")
def test_agent_copy_scaffold_preserves_mars_toml_mode(tmp_path: Path) -> None:
    mars_toml = tmp_path / "mars.toml"
    mars_toml.write_text('[settings]\n', encoding="utf-8")
    mars_toml.chmod(0o644)

    assert maybe_scaffold_claude_agent_copy(tmp_path, ["claude"]) is True

    assert _mode(mars_toml) == 0o644


@pytest.mark.skipif(IS_WINDOWS, reason="POSIX file modes are not enforced on Windows")
def test_work_status_file_uses_user_file_permissions(tmp_path: Path) -> None:
    previous_umask = os.umask(0o022)
    try:
        work_repository.create_work_item(tmp_path, "permission-policy")
    finally:
        os.umask(previous_umask)
    status_path = tmp_path / "work" / "permission-policy" / "__status.json"

    assert _mode(status_path) == 0o644
