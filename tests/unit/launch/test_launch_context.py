from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from meridian.lib.core.child_env import ALLOWED_CHILD_ENV_KEYS
from meridian.lib.core.types import HarnessId
from meridian.lib.harness.registry import get_default_harness_registry
from meridian.lib.launch.composition import PromptDocument
from meridian.lib.launch.context import build_launch_context
from meridian.lib.launch.request import (
    LaunchArgvIntent,
    LaunchCompositionSurface,
    LaunchRuntime,
    SpawnRequest,
)

if TYPE_CHECKING:
    from pytest import MonkeyPatch


def _build_spawn_request(
    prompt: str = "hello",
    extra_args: tuple[str, ...] = (),
) -> SpawnRequest:
    return SpawnRequest(
        model="gpt-5.4",
        harness=HarnessId.CODEX.value,
        prompt=prompt,
        extra_args=extra_args,
    )


def _build_primary_spawn_request(
    *,
    supplemental_prompt_documents: tuple[PromptDocument, ...] = (),
) -> SpawnRequest:
    return SpawnRequest(
        model="gpt-5.4",
        harness=HarnessId.CODEX.value,
        prompt="# Meridian Session",
        supplemental_prompt_documents=supplemental_prompt_documents,
    )


def _build_launch_runtime(
    *,
    tmp_path: Path,
    argv_intent: LaunchArgvIntent = LaunchArgvIntent.REQUIRED,
    composition_surface: LaunchCompositionSurface = LaunchCompositionSurface.DIRECT,
    execution_cwd: Path | None = None,
) -> LaunchRuntime:
    resolved_execution_cwd = execution_cwd or tmp_path
    return LaunchRuntime(
        argv_intent=argv_intent,
        composition_surface=composition_surface,
        report_output_path=(tmp_path / "report.md").as_posix(),
        runtime_root=(tmp_path / ".meridian").as_posix(),
        project_paths_project_root=tmp_path.as_posix(),
        project_paths_execution_cwd=resolved_execution_cwd.as_posix(),
    )


def _write_minimal_mars_config(project_root: Path) -> None:
    (project_root / "mars.toml").write_text(
        "[settings]\n"
        'targets = [".claude"]\n',
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    (
        "extra_args",
        "argv_intent",
        "patch_argv_failure",
        "expected_argv",
        "compare_dry_run",
    ),
    [
        pytest.param(
            (),
            LaunchArgvIntent.REQUIRED,
            False,
            None,
            True,
            id="raw-request-runtime-dry-run-share-argv",
        ),
        pytest.param(
            (),
            LaunchArgvIntent.SPEC_ONLY,
            True,
            (),
            False,
            id="spec-only-tolerates-argv-build-failure",
        ),
    ],
)
def test_build_launch_context_behaviors(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
    extra_args: tuple[str, ...],
    argv_intent: LaunchArgvIntent,
    patch_argv_failure: bool,
    expected_argv: tuple[str, ...] | None,
    compare_dry_run: bool,
) -> None:
    request = _build_spawn_request(extra_args=extra_args)
    runtime = _build_launch_runtime(
        tmp_path=tmp_path,
        argv_intent=argv_intent,
    )
    registry = get_default_harness_registry()

    if patch_argv_failure:
        def fail_build_launch_argv(**_: object) -> tuple[str, ...]:
            raise RuntimeError("argv unavailable")

        monkeypatch.setattr(
            "meridian.lib.launch.context.build_launch_argv",
            fail_build_launch_argv,
        )

    runtime_ctx = build_launch_context(
        spawn_id="p-ctx",
        request=request,
        runtime=runtime,
        harness_registry=registry,
        dry_run=False,
    )

    if compare_dry_run:
        dry_run_ctx = build_launch_context(
            spawn_id="p-ctx",
            request=request,
            runtime=runtime,
            harness_registry=registry,
            dry_run=True,
        )
        assert runtime_ctx.argv == dry_run_ctx.argv

    if expected_argv is not None:
        assert runtime_ctx.argv == expected_argv

    if patch_argv_failure:
        assert runtime_ctx.spec is not None


def test_build_launch_context_projects_runtime_child_env_paths(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("MERIDIAN_CHAT_ID", "c-parent")
    monkeypatch.setenv("MERIDIAN_DEPTH", "2")
    monkeypatch.setenv("MERIDIAN_ACTIVE_WORK_ID", "work-alpha")
    request = _build_spawn_request()
    runtime = _build_launch_runtime(tmp_path=tmp_path)

    runtime_ctx = build_launch_context(
        spawn_id="p-child-env",
        request=request,
        runtime=runtime,
        harness_registry=get_default_harness_registry(),
        dry_run=True,
    )

    assert runtime_ctx.env_overrides["MERIDIAN_DEPTH"] == "3"
    assert runtime_ctx.env_overrides["MERIDIAN_SPAWN_ID"] == "p-child-env"
    assert runtime_ctx.env_overrides["MERIDIAN_CHAT_ID"] == "c-parent"
    assert runtime_ctx.env_overrides["MERIDIAN_PROJECT_DIR"] == tmp_path.as_posix()
    assert runtime_ctx.env_overrides["MERIDIAN_RUNTIME_DIR"] == (
        tmp_path / ".meridian"
    ).as_posix()
    assert runtime_ctx.env_overrides["MERIDIAN_ACTIVE_WORK_ID"] == "work-alpha"
    assert runtime_ctx.env_overrides["MERIDIAN_ACTIVE_WORK_DIR"] == (
        tmp_path / ".meridian" / "work" / "work-alpha"
    ).as_posix()
    assert runtime_ctx.env_overrides["MERIDIAN_CONTEXT_WORK_DIR"] == (
        tmp_path / ".meridian" / "work"
    ).as_posix()
    assert runtime_ctx.env_overrides["MERIDIAN_CONTEXT_KB_DIR"] == (
        tmp_path / ".meridian" / "kb"
    ).as_posix()
    assert runtime_ctx.env_overrides["MERIDIAN_CONTEXT_WORK_ARCHIVE_DIR"] == (
        tmp_path / ".meridian" / "archive" / "work"
    ).as_posix()
    # MERIDIAN_HARNESS is informational (yield timing), not a policy override.
    assert runtime_ctx.env_overrides["MERIDIAN_HARNESS"] == "codex"
    assert runtime_ctx.env["MERIDIAN_HARNESS"] == "codex"
    unexpected = {
        key
        for key in runtime_ctx.env_overrides
        if key not in ALLOWED_CHILD_ENV_KEYS
        and not key.startswith("MERIDIAN_CONTEXT_")
        and key != "MERIDIAN_HARNESS"
    }
    assert unexpected == set()


@pytest.mark.parametrize(
    ("parent_depth", "expected_depth"),
    [
        pytest.param(None, "0", id="clean-shell-primary-root"),
        pytest.param("2", "2", id="primary-launched-from-existing-depth"),
    ],
)
def test_build_launch_context_primary_preserves_runtime_depth(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
    parent_depth: str | None,
    expected_depth: str,
) -> None:
    _write_minimal_mars_config(tmp_path)
    monkeypatch.delenv("MERIDIAN_DEPTH", raising=False)
    monkeypatch.delenv("MERIDIAN_SPAWN_ID", raising=False)
    if parent_depth is not None:
        monkeypatch.setenv("MERIDIAN_DEPTH", parent_depth)
    request = _build_spawn_request()
    runtime = _build_launch_runtime(
        tmp_path=tmp_path,
        composition_surface=LaunchCompositionSurface.PRIMARY,
    )

    runtime_ctx = build_launch_context(
        spawn_id="p-primary",
        request=request,
        runtime=runtime,
        harness_registry=get_default_harness_registry(),
        dry_run=True,
    )

    assert runtime_ctx.env_overrides["MERIDIAN_DEPTH"] == expected_depth
    assert runtime_ctx.env_overrides["MERIDIAN_SPAWN_ID"] == "p-primary"
    assert "MERIDIAN_PARENT_SPAWN_ID" not in runtime_ctx.env_overrides


def test_build_launch_context_primary_projects_supplemental_documents(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    _write_minimal_mars_config(tmp_path)
    request = _build_primary_spawn_request(
        supplemental_prompt_documents=(
            PromptDocument(
                kind="bootstrap",
                logical_name="setup",
                path="/setup/BOOTSTRAP.md",
                content="# Bootstrap: setup\n\nsetup docs",
            ),
        )
    )
    runtime = _build_launch_runtime(
        tmp_path=tmp_path,
        composition_surface=LaunchCompositionSurface.PRIMARY,
    )

    runtime_ctx = build_launch_context(
        spawn_id="p-primary-docs",
        request=request,
        runtime=runtime,
        harness_registry=get_default_harness_registry(),
        dry_run=True,
    )

    assert "# Bootstrap: setup\n\nsetup docs" in runtime_ctx.run_params.appended_system_prompt


def test_build_launch_context_primary_exports_configured_context_dirs(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    _write_minimal_mars_config(tmp_path)
    monkeypatch.delenv("MERIDIAN_ACTIVE_WORK_ID", raising=False)
    (tmp_path / "meridian.local.toml").write_text(
        "\n".join(
            [
                "[context.work]",
                'path = "ctx/work"',
                'archive = "ctx/archive/work"',
                "",
                "[context.kb]",
                'path = "ctx/kb"',
                "",
                "[context.strategy]",
                'path = "ctx/strategy"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    request = _build_spawn_request()
    runtime = _build_launch_runtime(
        tmp_path=tmp_path,
        composition_surface=LaunchCompositionSurface.PRIMARY,
    )

    runtime_ctx = build_launch_context(
        spawn_id="p-primary-context",
        request=request,
        runtime=runtime,
        harness_registry=get_default_harness_registry(),
        dry_run=True,
    )

    assert runtime_ctx.env_overrides["MERIDIAN_CONTEXT_WORK_DIR"] == (
        tmp_path / "ctx/work"
    ).as_posix()
    assert runtime_ctx.env_overrides["MERIDIAN_CONTEXT_WORK_ARCHIVE_DIR"] == (
        tmp_path / "ctx/archive/work"
    ).as_posix()
    assert runtime_ctx.env_overrides["MERIDIAN_CONTEXT_KB_DIR"] == (tmp_path / "ctx/kb").as_posix()
    assert runtime_ctx.env_overrides["MERIDIAN_CONTEXT_STRATEGY_DIR"] == (
        tmp_path / "ctx/strategy"
    ).as_posix()


def test_build_launch_context_env_keeps_project_root_when_execution_cwd_differs(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("MERIDIAN_DEPTH", "1")
    execution_cwd = tmp_path / ".meridian" / "spawns" / "p-parent"
    execution_cwd.mkdir(parents=True)
    request = _build_spawn_request()
    runtime = _build_launch_runtime(tmp_path=tmp_path, execution_cwd=execution_cwd)

    runtime_ctx = build_launch_context(
        spawn_id="p-child-env",
        request=request,
        runtime=runtime,
        harness_registry=get_default_harness_registry(),
        dry_run=True,
    )

    assert runtime_ctx.execution_cwd == execution_cwd
    assert runtime_ctx.env_overrides["MERIDIAN_PROJECT_DIR"] == tmp_path.as_posix()
    assert runtime_ctx.env_overrides["MERIDIAN_CONTEXT_KB_DIR"] == (
        tmp_path / ".meridian" / "kb"
    ).as_posix()


def test_build_launch_context_emits_child_spawn_id(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("MERIDIAN_SPAWN_ID", "p-parent")
    monkeypatch.setenv("MERIDIAN_DEPTH", "1")
    request = _build_spawn_request()
    runtime = _build_launch_runtime(tmp_path=tmp_path)

    runtime_ctx = build_launch_context(
        spawn_id="p-child",
        request=request,
        runtime=runtime,
        harness_registry=get_default_harness_registry(),
        dry_run=True,
    )

    assert runtime_ctx.env_overrides["MERIDIAN_SPAWN_ID"] == "p-child"
    assert runtime_ctx.env_overrides["MERIDIAN_PARENT_SPAWN_ID"] == "p-parent"


def test_build_launch_context_projects_context_paths_to_workspace_roots(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    """CONTEXT-PROJ-1: Context paths are included in workspace projection."""
    _write_minimal_mars_config(tmp_path)
    monkeypatch.delenv("MERIDIAN_ACTIVE_WORK_ID", raising=False)
    (tmp_path / "meridian.local.toml").write_text(
        "\n".join(
            [
                "[context.work]",
                'path = "ctx/work"',
                'archive = "ctx/archive/work"',
                "",
                "[context.kb]",
                'path = "ctx/kb"',
                "",
                "[context.strategy]",
                'path = "ctx/strategy"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    # Create the directories so they exist for projection
    (tmp_path / "ctx" / "work").mkdir(parents=True)
    (tmp_path / "ctx" / "archive" / "work").mkdir(parents=True)
    (tmp_path / "ctx" / "kb").mkdir(parents=True)
    (tmp_path / "ctx" / "strategy").mkdir(parents=True)

    request = _build_spawn_request()
    runtime = _build_launch_runtime(
        tmp_path=tmp_path,
        composition_surface=LaunchCompositionSurface.PRIMARY,
    )

    runtime_ctx = build_launch_context(
        spawn_id="p-primary-context-proj",
        request=request,
        runtime=runtime,
        harness_registry=get_default_harness_registry(),
        dry_run=True,
    )

    # Verify env vars are still exported (existing behavior)
    assert runtime_ctx.env_overrides["MERIDIAN_CONTEXT_WORK_DIR"] == (
        tmp_path / "ctx" / "work"
    ).as_posix()
    assert runtime_ctx.env_overrides["MERIDIAN_CONTEXT_WORK_ARCHIVE_DIR"] == (
        tmp_path / "ctx" / "archive" / "work"
    ).as_posix()
    assert runtime_ctx.env_overrides["MERIDIAN_CONTEXT_KB_DIR"] == (
        tmp_path / "ctx" / "kb"
    ).as_posix()
    assert runtime_ctx.env_overrides["MERIDIAN_CONTEXT_STRATEGY_DIR"] == (
        tmp_path / "ctx" / "strategy"
    ).as_posix()

    # Verify workspace projection includes context paths for all harnesses.
    # For OpenCode: check OPENCODE_CONFIG_CONTENT env override.
    if "OPENCODE_CONFIG_CONTENT" in runtime_ctx.env_overrides:
        import json
        config = json.loads(runtime_ctx.env_overrides["OPENCODE_CONFIG_CONTENT"])
        external_dirs = config.get("permission", {}).get("external_directory", {})
        assert (tmp_path / "ctx" / "work").as_posix() in external_dirs
        assert (tmp_path / "ctx" / "kb").as_posix() in external_dirs
        assert (tmp_path / "ctx" / "strategy").as_posix() in external_dirs

    projected_roots = {path.as_posix() for path in runtime_ctx.spec.projected_roots}
    assert (tmp_path / "ctx" / "work").as_posix() in projected_roots
    assert (tmp_path / "ctx" / "archive" / "work").as_posix() in projected_roots
    assert (tmp_path / "ctx" / "kb").as_posix() in projected_roots
    assert (tmp_path / "ctx" / "strategy").as_posix() in projected_roots


def test_build_launch_context_opencode_includes_context_paths_in_external_directory(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    """CONTEXT-PROJ-2: OpenCode projection includes all context paths."""
    import json

    _write_minimal_mars_config(tmp_path)
    monkeypatch.delenv("MERIDIAN_ACTIVE_WORK_ID", raising=False)
    monkeypatch.delenv("OPENCODE_CONFIG_CONTENT", raising=False)
    (tmp_path / "meridian.local.toml").write_text(
        "\n".join(
            [
                "[context.work]",
                'path = "ctx/work"',
                'archive = "ctx/archive/work"',
                "",
                "[context.kb]",
                'path = "ctx/kb"',
                "",
                "[context.strategy]",
                'path = "ctx/strategy"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / "ctx" / "work").mkdir(parents=True)
    (tmp_path / "ctx" / "archive" / "work").mkdir(parents=True)
    (tmp_path / "ctx" / "kb").mkdir(parents=True)
    (tmp_path / "ctx" / "strategy").mkdir(parents=True)

    request = _build_spawn_request()
    request = request.model_copy(update={"harness": HarnessId.OPENCODE.value, "model": ""})
    runtime = _build_launch_runtime(
        tmp_path=tmp_path,
        composition_surface=LaunchCompositionSurface.PRIMARY,
    )

    runtime_ctx = build_launch_context(
        spawn_id="p-opencode-context-proj",
        request=request,
        runtime=runtime,
        harness_registry=get_default_harness_registry(),
        dry_run=True,
    )

    assert "OPENCODE_CONFIG_CONTENT" in runtime_ctx.env_overrides
    config = json.loads(runtime_ctx.env_overrides["OPENCODE_CONFIG_CONTENT"])
    external_dirs = config.get("permission", {}).get("external_directory", {})

    work_path = (tmp_path / "ctx" / "work").as_posix() + "/*"
    kb_path = (tmp_path / "ctx" / "kb").as_posix() + "/*"
    archive_path = (tmp_path / "ctx" / "archive" / "work").as_posix() + "/*"
    strategy_path = (tmp_path / "ctx" / "strategy").as_posix() + "/*"

    assert work_path in external_dirs
    assert kb_path in external_dirs
    assert archive_path in external_dirs
    assert strategy_path in external_dirs
    assert external_dirs[work_path] == "allow"
    assert external_dirs[kb_path] == "allow"
    assert external_dirs[archive_path] == "allow"
    assert external_dirs[strategy_path] == "allow"
