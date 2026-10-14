"""CLI integration: TOML anchors identity but never gates command use."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.support.executables import prepend_fake_executables

NO_PROJECT_MSG = "No Meridian project found. Run from the project root or pass -C <path>."


def _run_meridian(
    args: list[str],
    *,
    cwd: Path,
    meridian_home: Path,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    for key in tuple(env):
        if key.upper().startswith("MERIDIAN_"):
            env.pop(key, None)
    env["MERIDIAN_HOME"] = meridian_home.as_posix()
    return subprocess.run(
        [sys.executable, "-m", "meridian", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _assert_project_initialized(project_root: Path) -> None:
    assert (project_root / "meridian.toml").is_file()
    assert "[project]" in (project_root / "meridian.toml").read_text(encoding="utf-8")
    assert not (project_root / ".meridian").exists()


def _assert_project_not_initialized(project_root: Path) -> None:
    assert not (project_root / "meridian.toml").exists()
    assert not (project_root / ".meridian").exists()


def _assert_adhoc_materialized(project_root: Path, meridian_home: Path) -> None:
    _assert_project_not_initialized(project_root)
    origins = list((meridian_home / "projects").glob("adhoc.*/origin.json"))
    assert len(origins) == 1
    assert project_root.resolve().as_posix() in origins[0].read_text(encoding="utf-8")


@pytest.fixture
def meridian_home(tmp_path: Path) -> Path:
    home = tmp_path / "meridian-home"
    home.mkdir()
    return home


@pytest.fixture
def cwd_with_project_id(tmp_path: Path) -> Path:
    project_root = tmp_path / "project"
    project_root.mkdir()
    (project_root / "meridian.toml").write_text(
        '[project]\nid = "proj-cwd-integration"\n', encoding="utf-8"
    )
    return project_root


@pytest.fixture
def cwd_without_project_id(tmp_path: Path) -> Path:
    bare_cwd = tmp_path / "no-project"
    bare_cwd.mkdir()
    return bare_cwd


@pytest.fixture
def cwd_with_mars_only(tmp_path: Path) -> Path:
    project_root = tmp_path / "mars-only"
    project_root.mkdir()
    (project_root / "mars.toml").write_text("[settings]\n", encoding="utf-8")
    return project_root


@pytest.mark.integration
def test_spawn_list_succeeds_from_cwd_with_project_id(
    cwd_with_project_id: Path,
    meridian_home: Path,
) -> None:
    result = _run_meridian(["spawn", "list"], cwd=cwd_with_project_id, meridian_home=meridian_home)

    assert result.returncode == 0, result.stderr or result.stdout
    assert NO_PROJECT_MSG not in (result.stdout + result.stderr)


@pytest.mark.integration
def test_spawn_list_succeeds_without_project_id_and_creates_nothing(
    cwd_without_project_id: Path,
    meridian_home: Path,
) -> None:
    result = _run_meridian(
        ["spawn", "list"],
        cwd=cwd_without_project_id,
        meridian_home=meridian_home,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    assert NO_PROJECT_MSG not in (result.stdout + result.stderr)
    _assert_project_not_initialized(cwd_without_project_id)
    assert list(meridian_home.iterdir()) == []


@pytest.mark.integration
def test_context_observes_unmaterialized_adhoc_identity_without_writing(
    cwd_without_project_id: Path,
    meridian_home: Path,
) -> None:
    result = _run_meridian(
        ["context"],
        cwd=cwd_without_project_id,
        meridian_home=meridian_home,
    )

    assert result.returncode == 0, result.stderr
    assert "identity_tier: adhoc" in result.stdout
    assert "project_id: adhoc.no-project." in result.stdout
    assert "project_identity_source: path" in result.stdout
    assert "mars_root:" in result.stdout
    assert "mars_scope: user" in result.stdout
    assert "mars_user_root: (same as primary)" in result.stdout
    assert "mars_fallback: n/a" in result.stdout
    assert "Mars roots:" not in result.stdout
    _assert_project_not_initialized(cwd_without_project_id)
    assert list(meridian_home.iterdir()) == []


@pytest.mark.integration
def test_doctor_observes_bare_directory_without_writing(
    cwd_without_project_id: Path,
    meridian_home: Path,
) -> None:
    result = _run_meridian(
        ["doctor"],
        cwd=cwd_without_project_id,
        meridian_home=meridian_home,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    assert "identity_tier: adhoc" in result.stdout
    assert "project_state: no state here" in result.stdout
    assert "mars_roots.scope: user" in result.stdout
    assert list(cwd_without_project_id.iterdir()) == []
    assert list(meridian_home.iterdir()) == []


@pytest.mark.integration
def test_hooks_run_is_noop_without_materialized_state(
    cwd_without_project_id: Path,
    meridian_home: Path,
) -> None:
    (meridian_home / "config.toml").write_text(
        "[[hooks]]\n"
        'name = "manual-test"\n'
        'event = "spawn.finalized"\n'
        'command = "printf should-not-run"\n',
        encoding="utf-8",
    )

    result = _run_meridian(
        ["hooks", "run", "manual-test"],
        cwd=cwd_without_project_id,
        meridian_home=meridian_home,
    )

    assert result.returncode == 0, result.stderr
    assert "skip_reason: no_state" in result.stdout
    assert not (meridian_home / "projects").exists()
    _assert_project_not_initialized(cwd_without_project_id)


@pytest.mark.integration
def test_spawn_list_in_mars_only_project_does_not_create_meridian_toml(
    cwd_with_mars_only: Path,
    meridian_home: Path,
) -> None:
    before = {path.relative_to(cwd_with_mars_only) for path in cwd_with_mars_only.rglob("*")}
    result = _run_meridian(
        ["spawn", "list"], cwd=cwd_with_mars_only, meridian_home=meridian_home
    )
    after = {path.relative_to(cwd_with_mars_only) for path in cwd_with_mars_only.rglob("*")}

    assert result.returncode == 0, result.stderr or result.stdout
    assert after == before
    assert not (cwd_with_mars_only / "meridian.toml").exists()


@pytest.mark.integration
def test_init_succeeds_from_cwd_without_project_id(
    cwd_without_project_id: Path,
    meridian_home: Path,
) -> None:
    result = _run_meridian(
        ["--mode", "human", "init"],
        cwd=cwd_without_project_id,
        meridian_home=meridian_home,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    assert NO_PROJECT_MSG not in (result.stdout + result.stderr)
    _assert_project_initialized(cwd_without_project_id)


@pytest.mark.integration
def test_init_succeeds_for_explicit_path_from_cwd_without_project_id(
    cwd_without_project_id: Path,
    meridian_home: Path,
    tmp_path: Path,
) -> None:
    target = tmp_path / "target-project"

    result = _run_meridian(
        ["--mode", "human", "init", target.as_posix()],
        cwd=cwd_without_project_id,
        meridian_home=meridian_home,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    assert NO_PROJECT_MSG not in (result.stdout + result.stderr)
    _assert_project_initialized(target)
    _assert_project_not_initialized(cwd_without_project_id)


@pytest.mark.integration
def test_primary_launch_materializes_adhoc_state_without_litter(
    cwd_without_project_id: Path,
    meridian_home: Path,
) -> None:
    result = _run_meridian(
        [
            "--mode",
            "human",
            "--harness",
            "definitely-missing",
            "--timeout",
            "0.1",
        ],
        cwd=cwd_without_project_id,
        meridian_home=meridian_home,
    )

    assert result.returncode == 1
    assert NO_PROJECT_MSG not in (result.stdout + result.stderr)
    _assert_adhoc_materialized(cwd_without_project_id, meridian_home)
    assert "state is kept for this path only" in result.stderr


@pytest.mark.integration
@pytest.mark.parametrize(
    "args",
    [
        ["--mode", "human", "--harness", "opencode", "--agent", "", "--dry-run"],
        [
            "--mode",
            "human",
            "--harness",
            "opencode",
            "spawn",
            "preview run",
            "--dry-run",
        ],
    ],
    ids=["launch", "spawn"],
)
def test_dry_run_does_not_materialize_adhoc_state(
    args: list[str],
    cwd_without_project_id: Path,
    meridian_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepend_fake_executables(monkeypatch, tmp_path, "opencode")

    result = _run_meridian(
        args,
        cwd=cwd_without_project_id,
        meridian_home=meridian_home,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    _assert_project_not_initialized(cwd_without_project_id)
    assert not list((meridian_home / "projects").glob("adhoc.*"))


@pytest.mark.integration
@pytest.mark.parametrize(
    "args",
    [
        ["--mode", "human", "--harness", "opencode", "--agent", "", "--dry-run"],
        [
            "--mode",
            "human",
            "--harness",
            "opencode",
            "spawn",
            "preview run",
            "--dry-run",
        ],
    ],
    ids=["launch", "spawn"],
)
def test_dry_run_does_not_mint_committed_identity(
    args: list[str],
    cwd_with_mars_only: Path,
    meridian_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepend_fake_executables(monkeypatch, tmp_path, "opencode")

    result = _run_meridian(
        args,
        cwd=cwd_with_mars_only,
        meridian_home=meridian_home,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    assert not (cwd_with_mars_only / "meridian.toml").exists()
    assert not (meridian_home / "projects").exists()


@pytest.mark.integration
@pytest.mark.parametrize(
    "args",
    [
        ["--mode", "human", "work", "start", "first work"],
        ["--mode", "human", "--harness", "definitely-missing", "spawn", "-p", "hi"],
    ],
)
def test_runtime_writes_materialize_adhoc_state_without_litter(
    args: list[str],
    cwd_without_project_id: Path,
    meridian_home: Path,
) -> None:
    result = _run_meridian(
        args,
        cwd=cwd_without_project_id,
        meridian_home=meridian_home,
    )

    assert NO_PROJECT_MSG not in (result.stdout + result.stderr)
    _assert_adhoc_materialized(cwd_without_project_id, meridian_home)


@pytest.mark.integration
def test_materialization_notice_names_nearest_marked_ancestor(
    tmp_path: Path,
    meridian_home: Path,
) -> None:
    ancestor = tmp_path / "parent"
    project_root = ancestor / "scratch"
    project_root.mkdir(parents=True)
    (ancestor / "meridian.toml").write_text(
        '[project]\nid = "parent-project"\n', encoding="utf-8"
    )

    result = _run_meridian(
        ["--mode", "human", "work", "start", "first work"],
        cwd=project_root,
        meridian_home=meridian_home,
    )

    assert result.returncode == 0, result.stderr
    assert f"(nearest project: {ancestor.as_posix()})" in result.stderr
    assert list(project_root.iterdir()) == []

    second = _run_meridian(
        ["--mode", "human", "work", "switch", "first-work"],
        cwd=project_root,
        meridian_home=meridian_home,
    )
    assert second.returncode == 0, second.stderr
    assert "state is kept for this path only" not in second.stderr


@pytest.mark.integration
@pytest.mark.parametrize(
    "args",
    [
        ["--mode", "human", "config", "set", "defaults.max_depth", "7"],
        ["--mode", "human", "config", "reset", "defaults.max_depth"],
        ["--mode", "human", "workspace", "init"],
    ],
)
def test_project_authoring_errors_without_a_marker(
    args: list[str],
    cwd_without_project_id: Path,
    meridian_home: Path,
) -> None:
    result = _run_meridian(args, cwd=cwd_without_project_id, meridian_home=meridian_home)

    assert result.returncode == 1
    assert "no project config here; run 'meridian init'" in result.stderr
    _assert_project_not_initialized(cwd_without_project_id)
    assert list(meridian_home.iterdir()) == []


@pytest.mark.integration
def test_config_init_still_commits_the_directory(
    cwd_without_project_id: Path,
    meridian_home: Path,
) -> None:
    result = _run_meridian(
        ["--mode", "human", "config", "init"],
        cwd=cwd_without_project_id,
        meridian_home=meridian_home,
    )

    assert result.returncode == 0, result.stderr
    _assert_project_initialized(cwd_without_project_id)


@pytest.mark.integration
@pytest.mark.parametrize(
    "args",
    [
        ["--mode", "human", "work", "list"],
    ],
)
def test_read_commands_do_not_auto_initialize_cwd_without_project_id(
    args: list[str],
    cwd_without_project_id: Path,
    meridian_home: Path,
) -> None:
    result = _run_meridian(
        args,
        cwd=cwd_without_project_id,
        meridian_home=meridian_home,
    )

    assert NO_PROJECT_MSG not in (result.stdout + result.stderr)
    _assert_project_not_initialized(cwd_without_project_id)


@pytest.mark.integration
def test_primary_launch_preserves_existing_config_identity(
    cwd_with_project_id: Path,
    meridian_home: Path,
) -> None:
    result = _run_meridian(
        [
            "--mode",
            "human",
            "--harness",
            "definitely-missing",
            "--timeout",
            "0.1",
        ],
        cwd=cwd_with_project_id,
        meridian_home=meridian_home,
    )

    assert result.returncode == 1
    assert NO_PROJECT_MSG not in (result.stdout + result.stderr)
    content = (cwd_with_project_id / "meridian.toml").read_text(encoding="utf-8")
    assert 'id = "proj-cwd-integration"' in content


@pytest.mark.integration
def test_doctor_runs_from_cwd_with_project_id(
    cwd_with_project_id: Path,
    meridian_home: Path,
) -> None:
    result = _run_meridian(["doctor"], cwd=cwd_with_project_id, meridian_home=meridian_home)

    assert result.returncode == 0, result.stderr or result.stdout
    assert NO_PROJECT_MSG not in (result.stdout + result.stderr)
    assert "project_root:" in result.stdout


@pytest.mark.integration
def test_doctor_runs_from_cwd_without_project_id(
    cwd_without_project_id: Path,
    meridian_home: Path,
) -> None:
    result = _run_meridian(["doctor"], cwd=cwd_without_project_id, meridian_home=meridian_home)

    assert result.returncode == 0, result.stderr or result.stdout
    assert NO_PROJECT_MSG not in (result.stdout + result.stderr)
    assert "project_root:" in result.stdout
