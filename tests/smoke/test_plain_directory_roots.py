"""Smoke tests for plain-directory root authority discovery."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


def _base_env(tmp_path: Path) -> dict[str, str]:
    keep = [
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "TMP",
        "TEMP",
        "HOME",
        "USERPROFILE",
        "LOCALAPPDATA",
    ]
    env = {key: os.environ[key] for key in keep if key in os.environ}
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["MERIDIAN_HOME"] = str(tmp_path / "home")
    return env


def test_plain_directory_nested_config_show_and_workspace_init_use_same_authority(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "plain-project"
    nested = project_root / "src" / "feature"
    project_root.mkdir()
    nested.mkdir(parents=True)
    (project_root / "meridian.toml").write_text(
        "[defaults]\nharness = \"codex\"\n",
        encoding="utf-8",
    )

    repo_root = Path(__file__).resolve().parents[2]
    env = _base_env(tmp_path)

    show = subprocess.run(
        ["uv", "run", "--project", str(repo_root), "meridian", "--json", "config", "show"],
        cwd=nested,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert show.returncode == 0, show.stderr
    payload = json.loads(show.stdout)
    assert payload["project_root"] == project_root.resolve().as_posix()

    init = subprocess.run(
        ["uv", "run", "--project", str(repo_root), "meridian", "workspace", "init"],
        cwd=nested,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert init.returncode == 0, init.stderr
    assert (project_root / "meridian.local.toml").is_file()
