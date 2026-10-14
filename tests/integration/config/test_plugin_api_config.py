from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from meridian.plugin_api.config import get_user_config
from meridian.plugin_api.git import resolve_clone_path

if TYPE_CHECKING:
    import pytest


def test_plugin_config_uses_config_env_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    default_home = tmp_path / "home"
    default_home.mkdir()
    (default_home / "config.toml").write_text(
        '[git."https://example.com/team/repo.git"]\npath = "/wrong/path"\n',
        encoding="utf-8",
    )
    override_clone = tmp_path / "override-clone"
    override_config = tmp_path / "override.toml"
    override_config.write_text(
        (
            '[git."https://example.com/team/repo.git"]\n'
            f'path = "{override_clone.as_posix()}"\n'
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("MERIDIAN_HOME", default_home.as_posix())
    monkeypatch.setenv("MERIDIAN_CONFIG", override_config.as_posix())

    assert get_user_config()["git"]["https://example.com/team/repo.git"]["path"] == str(
        override_clone
    )
    assert resolve_clone_path("https://example.com/team/repo") == override_clone
