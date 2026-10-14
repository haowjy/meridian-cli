from pathlib import Path

import pytest

from meridian.lib.config.loader import OPTION_CATALOG, load_config
from meridian.lib.config.models import MeridianConfig


def test_ignore_direnv_config_layers_and_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MERIDIAN_IGNORE_DIRENV", raising=False)
    user = tmp_path / "user.toml"
    user.write_text("[environment]\nignore_direnv = true\n")
    assert load_config(tmp_path, user_config=user).environment.ignore_direnv is True
    (tmp_path / "meridian.toml").write_text("[environment]\nignore_direnv = false\n")
    assert load_config(tmp_path, user_config=user).environment.ignore_direnv is False
    (tmp_path / "meridian.local.toml").write_text("[environment]\nignore_direnv = true\n")
    assert load_config(tmp_path, user_config=user).environment.ignore_direnv is True
    monkeypatch.setenv("MERIDIAN_IGNORE_DIRENV", "false")
    config = load_config(tmp_path, user_config=user)
    assert config.environment.ignore_direnv is False
    monkeypatch.setenv("MERIDIAN_IGNORE_DIRENV", "true")
    assert MeridianConfig.model_validate(config.model_dump()).environment.ignore_direnv is False
    assert OPTION_CATALOG.resolve_key("environment.ignore_direnv").primary_env_var == (
        "MERIDIAN_IGNORE_DIRENV"
    )
