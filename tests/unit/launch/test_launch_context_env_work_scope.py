# qa-validated: test-suite-redesign
"""Work-scope rebinding: child ambient dirs, named work, bind-time context-env refresh."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import meridian.lib.harness.cursor as cursor_harness
from meridian.lib.core.types import HarnessId
from meridian.lib.harness.registry import get_default_harness_registry
from meridian.lib.launch import context as launch_context_module
from meridian.lib.launch.context import build_launch_context
from meridian.lib.launch.prompt_context import CONTEXT_PROMPT_HEADER
from meridian.lib.launch.request import LaunchCompositionSurface
from tests.support.launch import stub_bundle_request_and_resolve
from tests.unit.launch.context_env_helpers import (
    assert_materialized_work_dir_parity,
    build_launch_runtime,
    build_spawn_request,
    write_codex_subagent_profile,
    write_minimal_mars_config,
)

if TYPE_CHECKING:
    from pytest import MonkeyPatch


def test_build_launch_context_primary_exports_configured_context_dirs(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    write_minimal_mars_config(tmp_path)
    stub_bundle_request_and_resolve(
        monkeypatch,
        model="gpt-5.4",
        harness=HarnessId.CODEX,
    )
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

    runtime_ctx = build_launch_context(
        spawn_id="p-primary-context",
        request=build_spawn_request(),
        runtime=build_launch_runtime(
            tmp_path=tmp_path,
            composition_surface=LaunchCompositionSurface.PRIMARY,
        ),
        harness_registry=get_default_harness_registry(),
        dry_run=True,
    )

    bind_env = runtime_ctx.binding.environment.bind_env_overrides
    assert bind_env["MERIDIAN_CONTEXT_WORK_DIR"] == (tmp_path / "ctx/work").as_posix()
    assert (
        bind_env["MERIDIAN_CONTEXT_WORK_ARCHIVE_DIR"] == (tmp_path / "ctx/archive/work").as_posix()
    )
    assert bind_env["MERIDIAN_CONTEXT_KB_DIR"] == (tmp_path / "ctx/kb").as_posix()
    assert bind_env["MERIDIAN_CONTEXT_STRATEGY_DIR"] == (tmp_path / "ctx/strategy").as_posix()


def test_build_launch_context_opencode_includes_context_paths_in_external_directory(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    write_minimal_mars_config(tmp_path)
    stub_bundle_request_and_resolve(
        monkeypatch,
        model="gemini-2.5-pro",
        harness=HarnessId.OPENCODE,
    )
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

    request = build_spawn_request().model_copy(update={"harness": HarnessId.OPENCODE.value})
    runtime_ctx = build_launch_context(
        spawn_id="p-opencode-context-proj",
        request=request,
        runtime=build_launch_runtime(
            tmp_path=tmp_path,
            composition_surface=LaunchCompositionSurface.PRIMARY,
        ),
        harness_registry=get_default_harness_registry(),
        dry_run=True,
    )

    bind_env = runtime_ctx.binding.environment.bind_env_overrides
    config = json.loads(bind_env["OPENCODE_CONFIG_CONTENT"])
    external_dirs = config.get("permission", {}).get("external_directory", {})

    work_path = (tmp_path / "ctx" / "work").as_posix() + "/**"
    kb_path = (tmp_path / "ctx" / "kb").as_posix() + "/**"
    archive_path = (tmp_path / "ctx" / "archive" / "work").as_posix() + "/**"
    strategy_path = (tmp_path / "ctx" / "strategy").as_posix() + "/**"

    for path in (work_path, kb_path, archive_path, strategy_path):
        assert path in external_dirs
        assert external_dirs[path] == "allow"


def test_bind_launch_context_child_ambient_work_dir_matches_prompt(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    write_minimal_mars_config(tmp_path)
    # CI has no Cursor CLI on PATH; stub the preflight binary check (this test
    # covers ambient work-dir parity, not Cursor availability).
    monkeypatch.setattr(cursor_harness.shutil, "which", lambda _command: "/usr/bin/cursor")
    stub_bundle_request_and_resolve(
        monkeypatch,
        model="claude-opus-4-7-thinking-high",
        harness=HarnessId.CURSOR,
    )
    parent_ambient = tmp_path / ".meridian" / "spawns" / "p-parent" / "work"
    monkeypatch.setenv("MERIDIAN_SPAWN_ID", "p-parent")
    monkeypatch.setenv("_MERIDIAN_DEPTH", "1")
    monkeypatch.setenv("MERIDIAN_ACTIVE_WORK_DIR", parent_ambient.as_posix())
    monkeypatch.delenv("MERIDIAN_ACTIVE_WORK_ID", raising=False)

    from meridian.lib.state.paths import resolve_ambient_work_dir

    expected_child = resolve_ambient_work_dir(tmp_path, "p-child")
    request = build_spawn_request().model_copy(
        update={"harness": HarnessId.CURSOR.value, "prompt_is_composed": False},
    )
    runtime_ctx = build_launch_context(
        spawn_id="p-child",
        request=request,
        runtime=build_launch_runtime(
            tmp_path=tmp_path,
            composition_surface=LaunchCompositionSurface.SPAWN_PREPARE,
        ),
        harness_registry=get_default_harness_registry(),
        dry_run=True,
    )

    child_env = runtime_ctx.binding.environment.child_context_env
    assert "MERIDIAN_ACTIVE_WORK_ID" not in child_env

    run_params = runtime_ctx.binding.run_params
    assert_materialized_work_dir_parity(
        run_params_prompt=run_params.prompt,
        run_params_appended_system_prompt=run_params.appended_system_prompt or "",
        child_env=child_env,
        expected_work_dir=expected_child,
        parent_ambient=parent_ambient,
    )


def test_bind_launch_context_child_ambient_without_context_markers(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    write_minimal_mars_config(tmp_path)
    stub_bundle_request_and_resolve(
        monkeypatch,
        model="gpt-5.4",
        harness=HarnessId.CODEX,
    )
    parent_ambient = tmp_path / ".meridian" / "spawns" / "p-parent" / "work"
    monkeypatch.setenv("MERIDIAN_SPAWN_ID", "p-parent")
    monkeypatch.setenv("_MERIDIAN_DEPTH", "1")
    monkeypatch.setenv("MERIDIAN_ACTIVE_WORK_DIR", parent_ambient.as_posix())
    monkeypatch.delenv("MERIDIAN_ACTIVE_WORK_ID", raising=False)
    monkeypatch.setattr(launch_context_module, "build_context_prompt", lambda **_kwargs: None)

    from meridian.lib.state.paths import resolve_ambient_work_dir

    expected_child = resolve_ambient_work_dir(tmp_path, "p-child")
    custom_prompt = "CUSTOM_PROMPT_WITHOUT_CONTEXT_BLOCK"
    runtime_ctx = build_launch_context(
        spawn_id="p-child",
        request=build_spawn_request(prompt=custom_prompt).model_copy(
            update={"prompt_is_composed": False},
        ),
        runtime=build_launch_runtime(
            tmp_path=tmp_path,
            composition_surface=LaunchCompositionSurface.SPAWN_PREPARE,
        ),
        harness_registry=get_default_harness_registry(),
        dry_run=True,
    )

    child_env = runtime_ctx.binding.environment.child_context_env
    assert child_env["MERIDIAN_ACTIVE_WORK_DIR"] == expected_child.as_posix()
    assert child_env["MERIDIAN_ACTIVE_WORK_DIR"] != parent_ambient.as_posix()

    system_prompt = runtime_ctx.binding.run_params.appended_system_prompt or ""
    assert "Work coordination (meridian)" in system_prompt
    assert CONTEXT_PROMPT_HEADER not in system_prompt
    assert parent_ambient.as_posix() not in system_prompt
    assert runtime_ctx.binding.run_params.prompt == custom_prompt


def test_bind_launch_context_context_refresh_survives_header_footer_wording_change(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    write_minimal_mars_config(tmp_path)
    stub_bundle_request_and_resolve(
        monkeypatch,
        model="gpt-5.4",
        harness=HarnessId.CODEX,
    )
    parent_ambient = tmp_path / ".meridian" / "spawns" / "p-parent" / "work"
    monkeypatch.setenv("MERIDIAN_SPAWN_ID", "p-parent")
    monkeypatch.setenv("_MERIDIAN_DEPTH", "1")
    monkeypatch.setenv("MERIDIAN_ACTIVE_WORK_DIR", parent_ambient.as_posix())
    monkeypatch.delenv("MERIDIAN_ACTIVE_WORK_ID", raising=False)

    from meridian.lib.state.paths import resolve_ambient_work_dir

    expected_child = resolve_ambient_work_dir(tmp_path, "p-child")
    original_build_context_prompt = launch_context_module.build_context_prompt
    revised_header = "# Workspace Context (revised wording)"
    revised_footer = "See also: meridian context -h"

    def build_context_with_revised_wording_at_bind(
        *,
        project_root: Path,
        active_work_dir: Path | None = None,
        **kwargs: object,
    ) -> str | None:
        rendered = original_build_context_prompt(
            project_root=project_root,
            active_work_dir=active_work_dir,
            **kwargs,  # type: ignore[arg-type]
        )
        if rendered is None or active_work_dir != expected_child:
            return rendered
        lines = rendered.splitlines()
        if not lines:
            return rendered
        lines[0] = revised_header
        lines[-1] = revised_footer
        return "\n".join(lines)

    monkeypatch.setattr(
        launch_context_module,
        "build_context_prompt",
        build_context_with_revised_wording_at_bind,
    )

    runtime_ctx = build_launch_context(
        spawn_id="p-child",
        request=build_spawn_request().model_copy(update={"prompt_is_composed": False}),
        runtime=build_launch_runtime(
            tmp_path=tmp_path,
            composition_surface=LaunchCompositionSurface.SPAWN_PREPARE,
        ),
        harness_registry=get_default_harness_registry(),
        dry_run=True,
    )

    child_env = runtime_ctx.binding.environment.child_context_env
    run_params = runtime_ctx.binding.run_params
    assert_materialized_work_dir_parity(
        run_params_prompt=run_params.prompt,
        run_params_appended_system_prompt=run_params.appended_system_prompt or "",
        child_env=child_env,
        expected_work_dir=expected_child,
        parent_ambient=parent_ambient,
    )

    materialized = run_params.appended_system_prompt or run_params.prompt
    assert revised_header in materialized
    assert revised_footer in materialized
    assert CONTEXT_PROMPT_HEADER not in materialized
    assert "Inspect or configure: meridian context -h" not in materialized


def test_bind_launch_context_named_work_dir_matches_prompt(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    write_minimal_mars_config(tmp_path)
    stub_bundle_request_and_resolve(
        monkeypatch,
        model="gpt-5.4",
        harness=HarnessId.CODEX,
    )
    monkeypatch.delenv("MERIDIAN_ACTIVE_WORK_ID", raising=False)
    monkeypatch.delenv("MERIDIAN_ACTIVE_WORK_DIR", raising=False)

    work_id = "feature-x"
    runtime_ctx = build_launch_context(
        spawn_id="p-child",
        request=build_spawn_request().model_copy(
            update={"work_id_hint": work_id, "prompt_is_composed": False},
        ),
        runtime=build_launch_runtime(
            tmp_path=tmp_path,
            composition_surface=LaunchCompositionSurface.SPAWN_PREPARE,
        ),
        harness_registry=get_default_harness_registry(),
        dry_run=True,
        runtime_work_id=work_id,
    )

    child_env = runtime_ctx.binding.environment.child_context_env
    assert child_env["MERIDIAN_ACTIVE_WORK_ID"] == work_id
    work_dir = Path(child_env["MERIDIAN_ACTIVE_WORK_DIR"])

    run_params = runtime_ctx.binding.run_params
    assert_materialized_work_dir_parity(
        run_params_prompt=run_params.prompt,
        run_params_appended_system_prompt=run_params.appended_system_prompt or "",
        child_env=child_env,
        expected_work_dir=work_dir,
    )


def test_bind_launch_context_composed_request_refreshes_child_work_dir(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    write_minimal_mars_config(tmp_path)
    stub_bundle_request_and_resolve(
        monkeypatch,
        model="gpt-5.4",
        harness=HarnessId.CODEX,
    )
    parent_ambient = tmp_path / ".meridian" / "spawns" / "p-parent" / "work"
    monkeypatch.setenv("MERIDIAN_SPAWN_ID", "p-parent")
    monkeypatch.setenv("_MERIDIAN_DEPTH", "1")
    monkeypatch.setenv("MERIDIAN_ACTIVE_WORK_DIR", parent_ambient.as_posix())
    monkeypatch.delenv("MERIDIAN_ACTIVE_WORK_ID", raising=False)

    from meridian.lib.launch.composition_spawn import (
        bind_spawn_launch_context,
        compose_spawn_launch_surface,
    )
    from meridian.lib.launch.context import RuntimeBindings
    from meridian.lib.state.paths import resolve_ambient_work_dir

    runtime = build_launch_runtime(
        tmp_path=tmp_path,
        composition_surface=LaunchCompositionSurface.SPAWN_PREPARE,
    )
    parent_ctx = build_launch_context(
        spawn_id="p-parent",
        request=build_spawn_request().model_copy(update={"prompt_is_composed": False}),
        runtime=runtime,
        harness_registry=get_default_harness_registry(),
        dry_run=True,
    )
    composed_request = parent_ctx.resolved_request
    assert composed_request.prompt_is_composed

    prepared = compose_spawn_launch_surface(
        request=composed_request,
        runtime=runtime,
        harness_registry=get_default_harness_registry(),
        dry_run=True,
    )

    expected_child = resolve_ambient_work_dir(tmp_path, "p-child")
    child_ctx = bind_spawn_launch_context(
        prepared=prepared,
        bindings=RuntimeBindings(
            spawn_id="p-child",
            dry_run=True,
        ),
        runtime=runtime,
        harness_registry=get_default_harness_registry(),
    )

    child_env = child_ctx.binding.environment.child_context_env
    run_params = child_ctx.binding.run_params
    assert_materialized_work_dir_parity(
        run_params_prompt=run_params.prompt,
        run_params_appended_system_prompt=run_params.appended_system_prompt or "",
        child_env=child_env,
        expected_work_dir=expected_child,
        parent_ambient=parent_ambient,
    )


def test_bind_launch_context_context_refresh_preserves_adhoc_agent_payload(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    write_minimal_mars_config(tmp_path)
    write_codex_subagent_profile(tmp_path)
    stub_bundle_request_and_resolve(
        monkeypatch,
        model="gpt-5.4",
        harness=HarnessId.CODEX,
    )
    parent_ambient = tmp_path / ".meridian" / "spawns" / "p-parent" / "work"
    monkeypatch.setenv("MERIDIAN_SPAWN_ID", "p-parent")
    monkeypatch.setenv("_MERIDIAN_DEPTH", "1")
    monkeypatch.setenv("MERIDIAN_ACTIVE_WORK_DIR", parent_ambient.as_posix())
    monkeypatch.delenv("MERIDIAN_ACTIVE_WORK_ID", raising=False)

    from meridian.lib.launch.composition_spawn import (
        bind_spawn_launch_context,
        compose_spawn_launch_surface,
    )
    from meridian.lib.launch.context import RuntimeBindings
    from meridian.lib.state.paths import resolve_ambient_work_dir

    runtime = build_launch_runtime(
        tmp_path=tmp_path,
        composition_surface=LaunchCompositionSurface.SPAWN_PREPARE,
    )
    prepared = compose_spawn_launch_surface(
        request=build_spawn_request().model_copy(
            update={
                "agent": "meridian-subagent",
                "prompt_is_composed": False,
            },
        ),
        runtime=runtime,
        harness_registry=get_default_harness_registry(),
        dry_run=True,
    )
    expected_adhoc = "NATIVE_AGENT_PROFILE_BODY_FOR_BIND_REFRESH."
    assert expected_adhoc in (prepared.prompt_payload.adhoc_agent_payload or "")

    parent_ctx = bind_spawn_launch_context(
        prepared=prepared,
        bindings=RuntimeBindings(
            spawn_id="p-parent",
            dry_run=True,
        ),
        runtime=runtime,
        harness_registry=get_default_harness_registry(),
    )
    assert expected_adhoc in (parent_ctx.binding.run_params.adhoc_agent_payload or "")

    child_ctx = bind_spawn_launch_context(
        prepared=prepared,
        bindings=RuntimeBindings(
            spawn_id="p-child",
            dry_run=True,
        ),
        runtime=runtime,
        harness_registry=get_default_harness_registry(),
    )

    expected_child = resolve_ambient_work_dir(tmp_path, "p-child")
    child_env = child_ctx.binding.environment.child_context_env
    run_params = child_ctx.binding.run_params
    assert_materialized_work_dir_parity(
        run_params_prompt=run_params.prompt,
        run_params_appended_system_prompt=run_params.appended_system_prompt or "",
        child_env=child_env,
        expected_work_dir=expected_child,
        parent_ambient=parent_ambient,
    )
    assert expected_adhoc in (run_params.adhoc_agent_payload or "")
