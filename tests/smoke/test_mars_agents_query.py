"""Opt-in check that the real ``mars`` binary accepts the query argv we send.

The mocked tests in ``tests/integration/ops/test_mars_agents_query.py`` pin
whatever command the helper builds; they cannot catch a command the installed
binary rejects (``--mode`` is an argument of ``mars agents``, not of the
``list`` subcommand). This module builds a real scratch project with real
agents and requires the parsed results, so a rejected argv fails instead of
merely printing a parser sentence.

Skipped when ``mars`` is not installed; not part of the default pytest gate
(``tests/smoke/`` is out of ``testpaths``).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from meridian.lib.ops import mars as mars_module
from meridian.lib.ops.mars import mars_agent_subagents, mars_list_subagents

pytestmark = pytest.mark.slow

_LEAD = "smoke-query-lead"
_CHILD = "smoke-query-child"


def _run_mars(executable: str, *args: str, cwd: Path) -> None:
    completed = subprocess.run(
        [executable, *args],
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr


def _install_scratch_agents(project_root: Path, package_root: Path) -> None:
    for name, mode, extra in (
        (_LEAD, "primary", f"subagents: [{_CHILD}]\n"),
        (_CHILD, "subagent", ""),
    ):
        agent_dir = package_root / "agents"
        agent_dir.mkdir(parents=True, exist_ok=True)
        (agent_dir / f"{name}.md").write_text(
            f"---\nname: {name}\nmode: {mode}\n{extra}---\n\n{name} body\n",
            encoding="utf-8",
        )
    (package_root / "mars.toml").write_text(
        'schema = 1\n\n[package]\nname = "smoke-query-package"\n'
        'version = "0.1.0"\ndescription = "scratch agents"\n',
        encoding="utf-8",
    )


def test_mars_query_commands_accepted_by_real_mars(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = mars_module.resolve_mars_executable()
    if executable is None:
        pytest.skip("mars binary not available")

    monkeypatch.setenv("HOME", (tmp_path / "home").as_posix())
    package_root = tmp_path / "package"
    project_root = tmp_path / "project"
    project_root.mkdir()
    _install_scratch_agents(project_root, package_root)

    _run_mars(executable, "init", cwd=project_root)
    _run_mars(executable, "add", package_root.as_posix(), cwd=project_root)
    _run_mars(executable, "sync", cwd=project_root)

    assert mars_agent_subagents(project_root, _LEAD) == (_CHILD,)
    listed = {entry.name: entry for entry in mars_list_subagents(project_root)}
    assert listed[_CHILD].scope == "root"
