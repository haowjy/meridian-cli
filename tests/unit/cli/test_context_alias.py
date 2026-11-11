import os
from pathlib import Path

import pytest

from meridian.cli._cmd_utils import resolve_context_alias


def _docs_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "docs"
    (root / "newkb").mkdir(parents=True)
    (root / "meridian.toml").write_text('[context.kb]\npath = "newkb"\n')
    for key in [k for k in os.environ if k.startswith("MERIDIAN_")]:
        monkeypatch.delenv(key)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("MERIDIAN_PROJECT_DIR", str(root))
    monkeypatch.setenv("MERIDIAN_CONTEXT_KB_DIR", str(tmp_path / "gone" / "kb"))
    monkeypatch.chdir(root)
    return root


def test_existing_directory_beats_alias(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _docs_repo(tmp_path, monkeypatch)
    (root / "kb").mkdir()
    assert resolve_context_alias(Path("kb")) == Path("kb")
    assert resolve_context_alias(Path("./kb")) == Path("kb")


def test_alias_reads_current_config_not_launch_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _docs_repo(tmp_path, monkeypatch)
    assert resolve_context_alias(Path("kb")) == root / "newkb"
