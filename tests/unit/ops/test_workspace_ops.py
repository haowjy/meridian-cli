from pathlib import Path

import pytest

from meridian.lib.ops.workspace import _has_workspace_section_or_scaffold


def test_has_workspace_section_or_scaffold_accepts_commented_scaffold(tmp_path: Path) -> None:
    config_path = tmp_path / "meridian.local.toml"
    config_path.write_text(
        '# comment\n# [workspace.example]\n# path = "../sibling"\n',
        encoding="utf-8",
    )

    assert _has_workspace_section_or_scaffold(config_path) is True


def test_has_workspace_section_or_scaffold_raises_on_invalid_toml(tmp_path: Path) -> None:
    config_path = tmp_path / "meridian.local.toml"
    config_path.write_text("[workspace.example\npath = \"../broken\"\n", encoding="utf-8")

    with pytest.raises(ValueError, match="Invalid TOML"):
        _has_workspace_section_or_scaffold(config_path)
