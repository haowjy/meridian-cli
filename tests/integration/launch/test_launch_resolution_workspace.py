# qa-validated: test-suite-redesign
"""Workspace root projection tests for Claude, Codex, and OpenCode launch contexts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from meridian.lib.harness.registry import get_default_harness_registry
from meridian.lib.launch.context import build_launch_context
from meridian.lib.launch.request import (
    LaunchArgvIntent,
    LaunchRuntime,
    SpawnRequest,
)
from meridian.lib.launch.workspace_projection import OPENCODE_CONFIG_CONTENT_ENV
from meridian.plugin_api.git import resolve_clone_path

pytestmark = pytest.mark.slow


def _write_minimal_mars_config(project_root: Path) -> None:
    (project_root / "mars.toml").write_text(
        '[settings]\ntargets = [".claude"]\n',
        encoding="utf-8",
    )


def test_workspace_roots_append_after_claude_preflight_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_minimal_mars_config(tmp_path)
    shared_root = tmp_path / "shared"
    shared_root.mkdir()
    (tmp_path / "meridian.local.toml").write_text(
        '[workspace.shared]\npath = "./shared"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("CLAUDECODE", "1")
    registry = get_default_harness_registry()

    preview = build_launch_context(
        spawn_id="dry-run-claude-workspace-order",
        request=SpawnRequest(
            prompt="workspace order",
            model="claude-sonnet-4-5",
            harness="claude",
            extra_args=("--user-tail", "1"),
        ),
        runtime=LaunchRuntime(
            argv_intent=LaunchArgvIntent.REQUIRED,
            runtime_root=(tmp_path / ".meridian").as_posix(),
            project_paths_project_root=tmp_path.as_posix(),
            project_paths_execution_cwd=tmp_path.as_posix(),
        ),
        harness_registry=registry,
        dry_run=True,
    )

    runtime_root = tmp_path / ".meridian"
    assert preview.binding.child_cwd == tmp_path
    args = preview.binding.run_params.extra_args
    assert args[:2] == ("--user-tail", "1")
    assert "--add-dir" not in args
    projected_roots = {path.as_posix() for path in preview.binding.spec.projected_roots}
    assert shared_root.as_posix() in projected_roots
    assert runtime_root.as_posix() in projected_roots
    assert shared_root.as_posix() in preview.binding.argv
    assert runtime_root.as_posix() in preview.binding.argv


def test_named_workspace_roots_project_through_codex_launch_context(tmp_path: Path) -> None:
    _write_minimal_mars_config(tmp_path)
    committed_root = tmp_path / "committed-root"
    local_override_root = tmp_path / "local-override-root"
    local_only_root = tmp_path / "local-only-root"
    for path in (committed_root, local_override_root, local_only_root):
        path.mkdir()
    (tmp_path / "meridian.toml").write_text(
        "[workspace.shared]\n"
        'path = "./committed-root"\n'
        "\n"
        "[workspace.local_only]\n"
        'path = "./local-only-root"\n',
        encoding="utf-8",
    )
    (tmp_path / "meridian.local.toml").write_text(
        "[workspace.shared]\n"
        'path = "./local-override-root"\n'
        "\n"
        "[workspace.local_only]\n"
        'path = "./local-only-root"\n',
        encoding="utf-8",
    )

    preview = build_launch_context(
        spawn_id="dry-run-codex-named-workspace",
        request=SpawnRequest(
            prompt="workspace projection",
            model="gpt-5.4",
            harness="codex",
        ),
        runtime=LaunchRuntime(
            argv_intent=LaunchArgvIntent.REQUIRED,
            runtime_root=(tmp_path / ".meridian").as_posix(),
            project_paths_project_root=tmp_path.as_posix(),
            project_paths_execution_cwd=tmp_path.as_posix(),
        ),
        harness_registry=get_default_harness_registry(),
        dry_run=True,
    )

    runtime_root = tmp_path / ".meridian"
    assert "--add-dir" not in preview.binding.run_params.extra_args
    projected_roots = {path.as_posix() for path in preview.binding.spec.projected_roots}
    assert local_override_root.as_posix() in projected_roots
    assert local_only_root.as_posix() in projected_roots
    assert runtime_root.as_posix() in projected_roots
    codex_add_dir_pairs = {
        (preview.binding.argv[index], preview.binding.argv[index + 1])
        for index, token in enumerate(preview.binding.argv[:-1])
        if token == "--add-dir"
    }
    assert ("--add-dir", local_override_root.as_posix()) in codex_add_dir_pairs
    assert ("--add-dir", local_only_root.as_posix()) in codex_add_dir_pairs
    assert ("--add-dir", runtime_root.as_posix()) in codex_add_dir_pairs


def test_git_backed_context_remote_projects_clone_root_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_minimal_mars_config(tmp_path)
    remote = "git@github.com:meridian-flow/docs.git"
    monkeypatch.setenv("MERIDIAN_HOME", (tmp_path / "user-home").as_posix())
    (tmp_path / "meridian.toml").write_text(
        "[context.work]\n"
        'source = "git"\n'
        f'remote = "{remote}"\n'
        'path = "work"\n'
        "\n"
        "[context.kb]\n"
        'source = "git"\n'
        f'remote = "{remote}"\n'
        'path = "kb"\n',
        encoding="utf-8",
    )

    registry = get_default_harness_registry()

    preview = build_launch_context(
        spawn_id="dry-run-codex-git-context-root",
        request=SpawnRequest(
            prompt="workspace projection",
            model="gpt-5.4",
            harness="codex",
        ),
        runtime=LaunchRuntime(
            argv_intent=LaunchArgvIntent.REQUIRED,
            runtime_root=(tmp_path / ".meridian").as_posix(),
            project_paths_project_root=tmp_path.as_posix(),
            project_paths_execution_cwd=tmp_path.as_posix(),
        ),
        harness_registry=registry,
        dry_run=True,
    )

    clone_root = resolve_clone_path(remote)
    runtime_root = tmp_path / ".meridian"
    projected_root_values = [path.as_posix() for path in preview.binding.spec.projected_roots]
    assert projected_root_values.count(clone_root.as_posix()) == 1
    assert projected_root_values.count(runtime_root.as_posix()) == 1

    monkeypatch.setenv("CLAUDECODE", "1")
    claude_preview = build_launch_context(
        spawn_id="dry-run-claude-git-context-root",
        request=SpawnRequest(
            prompt="workspace projection",
            model="claude-sonnet-4-5",
            harness="claude",
        ),
        runtime=LaunchRuntime(
            argv_intent=LaunchArgvIntent.REQUIRED,
            runtime_root=runtime_root.as_posix(),
            project_paths_project_root=tmp_path.as_posix(),
            project_paths_execution_cwd=tmp_path.as_posix(),
        ),
        harness_registry=registry,
        dry_run=True,
    )
    claude_clone_root_pairs = sum(
        1
        for index, token in enumerate(claude_preview.binding.argv[:-1])
        if token == "--add-dir" and claude_preview.binding.argv[index + 1] == clone_root.as_posix()
    )
    assert claude_clone_root_pairs == 1


def test_named_workspace_roots_project_through_opencode_launch_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Isolate from parent OpenCode session env for deterministic assertions.
    monkeypatch.delenv(OPENCODE_CONFIG_CONTENT_ENV, raising=False)
    _write_minimal_mars_config(tmp_path)
    docs_root = tmp_path / "docs-root"
    docs_root.mkdir()
    (tmp_path / "meridian.toml").write_text(
        '[workspace.docs]\npath = "./docs-root"\n',
        encoding="utf-8",
    )

    preview = build_launch_context(
        spawn_id="dry-run-opencode-named-workspace",
        request=SpawnRequest(
            prompt="workspace projection",
            model="kimi-k2.6",
            harness="opencode",
        ),
        runtime=LaunchRuntime(
            argv_intent=LaunchArgvIntent.REQUIRED,
            runtime_root=(tmp_path / ".meridian").as_posix(),
            project_paths_project_root=tmp_path.as_posix(),
            project_paths_execution_cwd=tmp_path.as_posix(),
        ),
        harness_registry=get_default_harness_registry(),
        dry_run=True,
    )

    runtime_root = tmp_path / ".meridian"
    bind_env = preview.binding.environment.bind_env_overrides
    payload = json.loads(bind_env[OPENCODE_CONFIG_CONTENT_ENV])
    external_dirs = payload["permission"]["external_directory"]
    assert external_dirs[docs_root.as_posix() + "/**"] == "allow"
    assert external_dirs[runtime_root.as_posix() + "/**"] == "allow"


@pytest.mark.parametrize(
    "parent_env_present",
    [False, True],
    ids=["without_parent_env", "with_parent_env"],
)
def test_opencode_workspace_projection_merges_parent_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    parent_env_present: bool,
) -> None:
    _write_minimal_mars_config(tmp_path)
    shared_root = tmp_path / "shared"
    shared_root.mkdir()
    (tmp_path / "meridian.local.toml").write_text(
        '[workspace.shared]\npath = "./shared"\n',
        encoding="utf-8",
    )
    if parent_env_present:
        monkeypatch.setenv(
            OPENCODE_CONFIG_CONTENT_ENV,
            '{"instructions":["/tmp/system.md"],'
            '"permission":{"external_directory":{"/existing/*":"ask"}}}',
        )
    else:
        monkeypatch.delenv(OPENCODE_CONFIG_CONTENT_ENV, raising=False)
    registry = get_default_harness_registry()

    preview = build_launch_context(
        spawn_id="dry-run-opencode-workspace-suppressed",
        request=SpawnRequest(
            prompt="workspace projection",
            model="kimi-k2.6",
            harness="opencode",
        ),
        runtime=LaunchRuntime(
            argv_intent=LaunchArgvIntent.REQUIRED,
            runtime_root=(tmp_path / ".meridian").as_posix(),
            project_paths_project_root=tmp_path.as_posix(),
            project_paths_execution_cwd=tmp_path.as_posix(),
        ),
        harness_registry=registry,
        dry_run=True,
    )

    warning_codes = {warning.code for warning in preview.warnings}
    runtime_root = tmp_path / ".meridian"
    bind_env = preview.binding.environment.bind_env_overrides
    payload = json.loads(bind_env[OPENCODE_CONFIG_CONTENT_ENV])
    external_dirs = payload["permission"]["external_directory"]
    assert external_dirs[shared_root.as_posix() + "/**"] == "allow"
    assert external_dirs[runtime_root.as_posix() + "/**"] == "allow"
    if parent_env_present:
        assert payload["instructions"] == ["/tmp/system.md"]
        assert external_dirs["/existing/*"] == "ask"
    assert "workspace_opencode_parent_env_suppressed" not in warning_codes
