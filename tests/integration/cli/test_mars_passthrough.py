"""Real CLI coverage for the Mars passthrough process boundary."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from meridian import __version__
from tests.conftest import posix_only


@pytest.mark.integration
@posix_only
def test_mars_passthrough_streams_and_propagates_exit_with_meridian_env(
    tmp_path: Path,
) -> None:
    runtime_root = tmp_path / "runtime"
    runtime_bin = runtime_root / "bin"
    runtime_bin.mkdir(parents=True)
    shutil.copy(Path(sys.prefix) / "pyvenv.cfg", runtime_root / "pyvenv.cfg")
    (runtime_root / "lib").symlink_to(Path(sys.prefix) / "lib")
    python = runtime_bin / "python"
    python.symlink_to(sys.executable)

    record_path = tmp_path / "mars-record.json"
    mars = runtime_bin / "mars"
    mars.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "with open(os.environ['MARS_RECORD'], 'w', encoding='utf-8') as record:\n"
        "    payload = {'argv': sys.argv[1:], "
        "'managed': os.environ.get('MERIDIAN_MANAGED'), "
        "'version': os.environ.get('MERIDIAN_VERSION')}\n"
        "    json.dump(payload, record)\n"
        "print('mars stdout', flush=True)\n"
        "print('mars stderr', file=sys.stderr, flush=True)\n"
        "raise SystemExit(7)\n",
        encoding="utf-8",
    )
    mars.chmod(0o755)

    env = os.environ.copy()
    env["MARS_RECORD"] = str(record_path)
    env["MERIDIAN_HOME"] = str(tmp_path / "meridian-home")
    env["_MERIDIAN_DEPTH"] = "1"
    env.pop("MERIDIAN_MANAGED", None)
    env.pop("MERIDIAN_PROJECT_DIR", None)
    result = subprocess.run(
        [str(python), "-m", "meridian", "mars", "models", "list"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=3,
        check=False,
    )

    assert result.returncode == 7
    assert result.stdout == "mars stdout\n"
    assert result.stderr == "mars stderr\n"
    assert json.loads(record_path.read_text()) == {
        "argv": ["models", "list"],
        "managed": "1",
        "version": __version__,
    }


@pytest.mark.integration
@posix_only
def test_mars_init_creates_nested_project_below_ancestor_mars_root(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    nested = project / "nested"
    nested.mkdir(parents=True)
    (project / "mars.toml").write_text("", encoding="utf-8")
    env = os.environ.copy()
    env["MERIDIAN_HOME"] = (tmp_path / "meridian-home").as_posix()
    env.pop("MERIDIAN_PROJECT_DIR", None)

    result = subprocess.run(
        [sys.executable, "-m", "meridian", "mars", "init"],
        cwd=nested,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    assert (nested / "mars.toml").is_file()


@pytest.mark.integration
@posix_only
def test_init_link_creates_nested_project_below_ancestor_mars_root(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    nested = project / "nested"
    nested.mkdir(parents=True)
    (project / "mars.toml").write_text("", encoding="utf-8")
    env = os.environ.copy()
    env["MERIDIAN_HOME"] = (tmp_path / "meridian-home").as_posix()
    env.pop("MERIDIAN_PROJECT_DIR", None)

    result = subprocess.run(
        [sys.executable, "-m", "meridian", "init", "--link", ".claude"],
        cwd=nested,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    assert (nested / "mars.toml").is_file()
    assert (nested / ".claude").is_dir()


@pytest.mark.integration
@posix_only
def test_writes_outside_a_project_need_user_and_reach_the_user_level(tmp_path: Path) -> None:
    from meridian.lib.catalog.mars_cli import resolve_mars_executable

    if resolve_mars_executable() is None:
        pytest.skip("mars binary not available")
    bare = tmp_path / "bare"
    elsewhere = tmp_path / "elsewhere"
    package = tmp_path / "package" / "skills" / "hello"
    for directory in (bare, elsewhere, package):
        directory.mkdir(parents=True)
    (package / "SKILL.md").write_text(
        "---\nname: hello\ndescription: Hello.\n---\nHello.\n", encoding="utf-8"
    )
    mars_home = tmp_path / "mars-home"
    env = {
        **os.environ,
        "HOME": (tmp_path / "home").as_posix(),
        "MERIDIAN_HOME": (tmp_path / "meridian-home").as_posix(),
        "MARS_HOME": mars_home.as_posix(),
        # Meridian's directory, not the shell's, decides where Mars looks for a project.
        "MERIDIAN_PROJECT_DIR": bare.as_posix(),
    }

    def meridian_mars(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "meridian", "mars", *args],
            cwd=elsewhere,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    # `add` would create a project here; Meridian refuses before Mars runs it.
    refused_add = meridian_mars("add", (tmp_path / "package").as_posix())
    assert refused_add.returncode == 1
    assert "no project Mars root here" in refused_add.stderr
    # Every other write Mars refuses itself, naming --user.
    refused_alias = meridian_mars("models", "alias", "fast", "gpt-5")
    assert refused_alias.returncode != 0
    assert "--user" in refused_alias.stderr
    assert not any(bare.iterdir())

    added = meridian_mars("--user", "add", (tmp_path / "package").as_posix())
    assert added.returncode == 0, added.stderr
    assert (mars_home / "mars.toml").is_file()
    assert not any(bare.iterdir())
