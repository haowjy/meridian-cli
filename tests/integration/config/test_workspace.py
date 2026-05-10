from pathlib import Path

import pytest

from meridian.lib.config.workspace import get_projectable_roots, resolve_workspace_snapshot
from meridian.lib.core.types import HarnessId
from meridian.lib.harness.registry import get_default_harness_registry
from meridian.lib.launch.context import build_launch_context
from meridian.lib.launch.request import (
    LaunchArgvIntent,
    LaunchCompositionSurface,
    LaunchRuntime,
    SpawnRequest,
)
from meridian.lib.launch.workspace import ensure_workspace_valid_for_launch
from tests.conftest import posix_only


@pytest.fixture(autouse=True)
def _clear_state_root_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MERIDIAN_RUNTIME_DIR", raising=False)


def _repo(tmp_path: Path) -> Path:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    return project_root


def _write_minimal_mars_config(project_root: Path) -> None:
    (project_root / "mars.toml").write_text(
        "[settings]\n"
        'targets = [".claude"]\n',
        encoding="utf-8",
    )


def _build_workspace_launch_context(
    *,
    project_root: Path,
    surface: LaunchCompositionSurface = LaunchCompositionSurface.DIRECT,
):
    request = SpawnRequest(
        model="gpt-5.4",
        harness=HarnessId.CODEX.value,
        prompt="hello",
    )
    runtime = LaunchRuntime(
        argv_intent=LaunchArgvIntent.SPEC_ONLY,
        composition_surface=surface,
        report_output_path=(project_root / "report.md").as_posix(),
        runtime_root=(project_root / ".meridian").as_posix(),
        project_paths_project_root=project_root.as_posix(),
        project_paths_execution_cwd=project_root.as_posix(),
    )
    return build_launch_context(
        spawn_id="p-workspace",
        request=request,
        runtime=runtime,
        harness_registry=get_default_harness_registry(),
        dry_run=True,
    )


def test_resolve_workspace_snapshot_is_none_when_workspace_config_absent(tmp_path: Path) -> None:
    project_root = _repo(tmp_path)

    snapshot = resolve_workspace_snapshot(project_root)

    assert snapshot.status == "none"
    assert snapshot.source_paths == ()
    assert snapshot.roots == ()
    assert snapshot.findings == ()


def test_workspace_snapshot_loads_named_committed_and_local_entries(tmp_path: Path) -> None:
    project_root = _repo(tmp_path)
    frontend_committed = tmp_path / "frontend-committed"
    frontend_local = tmp_path / "frontend-local"
    prompts = tmp_path / "prompts"
    data = tmp_path / "data"
    for path in (frontend_committed, frontend_local, prompts, data):
        path.mkdir()
    committed_path = project_root / "meridian.toml"
    local_path = project_root / "meridian.local.toml"
    committed_path.write_text(
        "[workspace.frontend]\n"
        'path = "../frontend-committed"\n'
        "\n"
        "[workspace.prompts]\n"
        'path = "../prompts"\n',
        encoding="utf-8",
    )
    local_path.write_text(
        "[workspace.frontend]\n"
        'path = "../frontend-local"\n'
        "\n"
        "[workspace.data]\n"
        'path = "../data"\n',
        encoding="utf-8",
    )

    snapshot = resolve_workspace_snapshot(project_root)

    assert snapshot.status == "present"
    assert snapshot.source_paths == (committed_path.resolve(), local_path.resolve())
    assert [root.name for root in snapshot.roots] == ["frontend", "prompts", "data"]
    assert [root.source for root in snapshot.roots] == ["merged", "committed", "local"]
    assert [root.resolved_path for root in snapshot.roots] == [
        frontend_local.resolve(),
        prompts.resolve(),
        data.resolve(),
    ]
    assert get_projectable_roots(snapshot) == (
        frontend_local.resolve(),
        prompts.resolve(),
        data.resolve(),
    )
    assert snapshot.findings == ()


def test_workspace_snapshot_reports_local_and_committed_missing_roots(tmp_path: Path) -> None:
    project_root = _repo(tmp_path)
    (project_root / "existing").mkdir()
    (project_root / "meridian.toml").write_text(
        "[workspace.committed_missing]\n"
        'path = "./missing-committed"\n'
        "\n"
        "[workspace.existing]\n"
        'path = "./existing"\n',
        encoding="utf-8",
    )
    (project_root / "meridian.local.toml").write_text(
        "[workspace.local_missing]\n"
        'path = "./missing-local"\n',
        encoding="utf-8",
    )

    snapshot = resolve_workspace_snapshot(project_root)

    assert get_projectable_roots(snapshot) == ((project_root / "existing").resolve(),)
    assert {finding.code for finding in snapshot.findings} == {
        "workspace_local_missing_root",
        "workspace_missing_root",
    }


def test_workspace_snapshot_invalid_named_schema_marks_invalid(tmp_path: Path) -> None:
    project_root = _repo(tmp_path)
    (project_root / "meridian.toml").write_text(
        "[workspace.Bad]\n"
        'path = "./root"\n',
        encoding="utf-8",
    )

    snapshot = resolve_workspace_snapshot(project_root)

    assert snapshot.status == "invalid"
    assert snapshot.findings[0].code == "workspace_invalid"
    assert "entry name 'Bad'" in snapshot.findings[0].message


def test_workspace_snapshot_unknown_entry_keys_are_findings(tmp_path: Path) -> None:
    project_root = _repo(tmp_path)
    (project_root / "root").mkdir()
    (project_root / "meridian.toml").write_text(
        "[workspace.root]\n"
        'path = "./root"\n'
        "enabled = true\n",
        encoding="utf-8",
    )

    snapshot = resolve_workspace_snapshot(project_root)

    assert snapshot.status == "present"
    assert snapshot.findings[0].code == "workspace_unknown_key"
    assert snapshot.findings[0].payload == {"keys": ["workspace.root.enabled"]}


@posix_only  # HOME env var is not used by Path.expanduser() on Windows (uses USERPROFILE)
def test_named_workspace_resolves_absolute_and_tilde_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = _repo(tmp_path)
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    tilde_root = fake_home / "tilde-workspace-root"
    tilde_root.mkdir()
    absolute_root = tmp_path / "absolute-workspace-root"
    absolute_root.mkdir()
    monkeypatch.setenv("HOME", fake_home.as_posix())
    (project_root / "meridian.local.toml").write_text(
        "[workspace.tilde]\n"
        'path = "~/tilde-workspace-root"\n'
        "\n"
        "[workspace.absolute]\n"
        f'path = "{absolute_root.as_posix()}"\n',
        encoding="utf-8",
    )

    snapshot = resolve_workspace_snapshot(project_root)

    assert [root.name for root in snapshot.roots] == ["tilde", "absolute"]
    assert snapshot.roots[0].resolved_path == tilde_root.resolve()
    assert snapshot.roots[1].resolved_path == absolute_root.resolve()
    assert get_projectable_roots(snapshot) == (tilde_root.resolve(), absolute_root.resolve())


def test_user_global_workspace_config_is_ignored_for_snapshot_and_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = _repo(tmp_path)
    user_workspace_root = tmp_path / "user-workspace-root"
    user_workspace_root.mkdir()
    user_config_path = tmp_path / "user-config.toml"
    user_config_path.write_text(
        "[workspace.user_docs]\n"
        f'path = "{user_workspace_root.as_posix()}"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("MERIDIAN_CONFIG", user_config_path.as_posix())

    snapshot = resolve_workspace_snapshot(project_root)
    runtime_ctx = _build_workspace_launch_context(project_root=project_root)

    assert snapshot.status == "none"
    assert snapshot.source_paths == ()
    assert snapshot.roots == ()
    assert snapshot.findings == ()
    bind_overrides = runtime_ctx.binding.environment.bind_env_overrides
    assert user_workspace_root.as_posix() not in bind_overrides.values()
    assert all(
        user_workspace_root.as_posix() not in arg
        for arg in runtime_ctx.binding.run_params.extra_args
    )


def test_workspace_entries_do_not_create_context_env_vars(tmp_path: Path) -> None:
    project_root = _repo(tmp_path)
    workspace_root = project_root / "workspace-docs"
    workspace_root.mkdir()
    (project_root / "meridian.toml").write_text(
        "[workspace.docs]\n"
        'path = "./workspace-docs"\n',
        encoding="utf-8",
    )

    snapshot = resolve_workspace_snapshot(project_root)
    runtime_ctx = _build_workspace_launch_context(project_root=project_root)

    assert get_projectable_roots(snapshot) == (workspace_root.resolve(),)
    assert "MERIDIAN_CONTEXT_DOCS_DIR" not in runtime_ctx.binding.environment.bind_env_overrides


def test_workspace_entries_do_not_enter_system_prompt_context(tmp_path: Path) -> None:
    project_root = _repo(tmp_path)
    _write_minimal_mars_config(project_root)
    workspace_root = project_root / "workspace-docs"
    workspace_root.mkdir()
    (project_root / "meridian.toml").write_text(
        "[workspace.docs]\n"
        'path = "./workspace-docs"\n',
        encoding="utf-8",
    )

    runtime_ctx = _build_workspace_launch_context(
        project_root=project_root,
        surface=LaunchCompositionSurface.PRIMARY,
    )
    system_prompt = runtime_ctx.binding.run_params.appended_system_prompt or ""

    assert "workspace-docs" not in system_prompt
    assert "MERIDIAN_CONTEXT_DOCS_DIR" not in system_prompt


def test_workspace_launch_validation_raises_for_invalid_workspace(tmp_path: Path) -> None:
    project_root = _repo(tmp_path)
    (project_root / "meridian.toml").write_text("[workspace.bad]\n", encoding="utf-8")

    with pytest.raises(ValueError, match=r"Invalid workspace config in meridian\.toml"):
        ensure_workspace_valid_for_launch(project_root)


def test_workspace_launch_validation_allows_absent_or_valid_workspace(tmp_path: Path) -> None:
    project_root = _repo(tmp_path)

    ensure_workspace_valid_for_launch(project_root)

    (project_root / "existing").mkdir()
    (project_root / "meridian.toml").write_text(
        "[workspace.existing]\n"
        'path = "./existing"\n',
        encoding="utf-8",
    )
    ensure_workspace_valid_for_launch(project_root)
