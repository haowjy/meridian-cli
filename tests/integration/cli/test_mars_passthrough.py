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
    meridian_home = tmp_path / "meridian-home"
    user_mars_root = meridian_home / "mars"
    user_mars_root.mkdir(parents=True)
    (user_mars_root / "mars.toml").write_text("", encoding="utf-8")
    env["MERIDIAN_HOME"] = str(meridian_home)
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
        "argv": ["models", "list", "--root", user_mars_root.as_posix()],
        "managed": "1",
        "version": __version__,
    }


def test_passthrough_injects_project_primary_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from meridian.cli.mars_passthrough import parse_mars_passthrough

    project = tmp_path / "project"
    nested = project / "nested"
    nested.mkdir(parents=True)
    (project / "mars.toml").write_text("", encoding="utf-8")
    monkeypatch.setenv("MERIDIAN_PROJECT_DIR", nested.as_posix())

    request = parse_mars_passthrough(["list"], executable="/fake/mars")

    assert request.mars_args == ("list", "--root", project.as_posix())
    assert not request.user_scope


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


def test_passthrough_user_flag_injects_user_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from meridian.cli.mars_passthrough import parse_mars_passthrough

    project = tmp_path / "project"
    project.mkdir()
    (project / "mars.toml").write_text("", encoding="utf-8")
    user_home = tmp_path / "user-home"
    monkeypatch.setenv("MERIDIAN_PROJECT_DIR", project.as_posix())
    monkeypatch.setenv("MERIDIAN_HOME", user_home.as_posix())

    request = parse_mars_passthrough(["--user", "sync"], executable="/fake/mars")

    assert request.mars_args == (
        "sync",
        "--root",
        (user_home / "mars").resolve().as_posix(),
    )
    assert request.user_scope


def test_passthrough_layers_project_inspection_but_not_mutation_or_user_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from meridian.cli.mars_passthrough import parse_mars_passthrough
    from meridian.lib.catalog import mars_roots

    project = tmp_path / "project"
    project.mkdir()
    (project / "mars.toml").write_text("", encoding="utf-8")
    user_home = tmp_path / "user-home"
    user_root = (user_home / "mars").resolve()
    monkeypatch.setenv("MERIDIAN_PROJECT_DIR", project.as_posix())
    monkeypatch.setenv("MERIDIAN_HOME", user_home.as_posix())
    monkeypatch.setattr(
        mars_roots,
        "mars_supports_fallback_root",
        lambda _executable: True,
    )

    agents = parse_mars_passthrough(["agents", "list"], executable="/fake/mars")
    mutation = parse_mars_passthrough(["sync"], executable="/fake/mars")
    user_agents = parse_mars_passthrough(["--user", "agents", "list"], executable="/fake/mars")

    assert agents.mars_args == (
        "agents",
        "list",
        "--root",
        project.as_posix(),
        "--fallback-root",
        user_root.as_posix(),
    )
    assert "--fallback-root" not in mutation.mars_args
    assert "--fallback-root" not in user_agents.mars_args


def test_mutation_gate_uses_meridian_project_dir_before_root_injection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from meridian.cli.mars_passthrough import parse_mars_passthrough

    bare = tmp_path / "bare"
    bare.mkdir()
    monkeypatch.setenv("MERIDIAN_PROJECT_DIR", bare.as_posix())
    monkeypatch.setenv("MERIDIAN_HOME", (tmp_path / "user-home").as_posix())

    with pytest.raises(ValueError) as exc_info:
        parse_mars_passthrough(["add", "local-package"], executable="/fake/mars")

    assert str(exc_info.value) == (
        "no project Mars root here; run 'meridian init --link', or use 'meridian mars --user …'"
    )
    assert not any(bare.iterdir())


def test_nested_mutation_gate_requires_user_scope_in_bare_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from meridian.cli.mars_passthrough import parse_mars_passthrough

    bare = tmp_path / "bare"
    bare.mkdir()
    user_root = tmp_path / "user-home" / "mars"
    monkeypatch.chdir(bare)
    monkeypatch.delenv("MERIDIAN_PROJECT_DIR", raising=False)
    monkeypatch.setenv("MERIDIAN_HOME", user_root.parent.as_posix())

    with pytest.raises(ValueError, match="no project Mars root here"):
        parse_mars_passthrough(["models", "alias", "fast", "gpt-5"], executable="/fake/mars")

    request = parse_mars_passthrough(
        ["--user", "models", "alias", "fast", "gpt-5"], executable="/fake/mars"
    )
    assert request.mars_args[-2:] == ("--root", user_root.as_posix())
    assert request.user_scope
    assert not any(bare.iterdir())
