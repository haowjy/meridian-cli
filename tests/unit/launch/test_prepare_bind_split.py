from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import meridian.lib.launch.context as launch_context
from meridian.lib.catalog.catalog_session import CatalogSession
from meridian.lib.catalog.model_aliases import AliasEntry
from meridian.lib.core.types import HarnessId, ModelId
from meridian.lib.harness.registry import get_default_harness_registry
from meridian.lib.launch.context import (
    PreparedLaunchSurface,
    RuntimeBindings,
    bind_launch_context,
    build_launch_context,
    prepare_launch_surface,
)
from meridian.lib.launch.request import (
    LaunchArgvIntent,
    LaunchCompositionSurface,
    LaunchRuntime,
    SpawnRequest,
)


def _write_minimal_mars_config(project_root: Path) -> None:
    (project_root / "mars.toml").write_text(
        "[settings]\n"
        'targets = [".claude"]\n',
        encoding="utf-8",
    )


def _build_runtime(
    *,
    tmp_path: Path,
    composition_surface: LaunchCompositionSurface,
) -> LaunchRuntime:
    return LaunchRuntime(
        argv_intent=LaunchArgvIntent.REQUIRED,
        composition_surface=composition_surface,
        report_output_path=(tmp_path / "report.md").as_posix(),
        runtime_root=(tmp_path / ".meridian").as_posix(),
        project_paths_project_root=tmp_path.as_posix(),
        project_paths_execution_cwd=tmp_path.as_posix(),
    )


def _build_request(*, prompt_is_composed: bool = False) -> SpawnRequest:
    return SpawnRequest(
        prompt="hello",
        prompt_is_composed=prompt_is_composed,
        model="gpt-5.4",
        harness=HarnessId.CODEX.value,
    )


def _forbidden(name: str):
    def _raise(*_args: object, **_kwargs: object) -> object:
        raise AssertionError(f"{name} should not be called in this phase")

    return _raise


def test_prepare_launch_surface_does_not_call_bind_phase_helpers(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _write_minimal_mars_config(tmp_path)
    catalog = CatalogSession(tmp_path)
    monkeypatch.setattr(
        catalog,
        "resolve_model",
        lambda _token: AliasEntry(
            alias="",
            model_id=ModelId("gpt-5.4"),
            resolved_harness=HarnessId.CODEX,
        ),
    )
    monkeypatch.setattr(catalog, "load_aliases", lambda: [])
    monkeypatch.setattr(catalog, "list_all_models", lambda: [])

    for helper_name in (
        "resolve_child_execution_cwd",
        "build_env_plan",
        "resolve_permission_pipeline",
        "resolve_launch_spec_stage",
        "build_launch_argv",
        "project_workspace_roots",
    ):
        monkeypatch.setattr(launch_context, helper_name, _forbidden(helper_name))

    prepared = prepare_launch_surface(
        request=_build_request(),
        runtime=_build_runtime(
            tmp_path=tmp_path,
            composition_surface=LaunchCompositionSurface.SPAWN_PREPARE,
        ),
        project_root=tmp_path,
        harness_registry=get_default_harness_registry(),
        catalog=catalog,
        dry_run=True,
    )

    assert prepared.request.prompt_is_composed is True
    assert prepared.request.harness == HarnessId.CODEX.value


def test_bind_launch_context_does_not_call_prepare_phase_helpers(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    registry = get_default_harness_registry()
    prepared = PreparedLaunchSurface(
        request=_build_request(prompt_is_composed=True),
        harness=registry.get_subprocess_harness(HarnessId.CODEX),
        seed_harness_session_id="seed-session",
        composition_warnings=(),
        loaded_references=(),
        profile_tools_for_deny_optout=(),
        has_profile_for_deny_optout=False,
        projected_content=None,
        model_selection=None,
        alias_catalog=None,
        agent_inventory_prompt=None,
        context_prompt=None,
        seed_session_args=(),
        launch_request=None,
    )
    for helper_name in (
        "load_reference_items",
        "resolve_policies",
        "scan_agent_profiles",
        "build_agent_inventory_prompt",
        "build_context_prompt",
        "resolve_skills_from_profile",
        "compose_skill_prompt_documents",
    ):
        monkeypatch.setattr(launch_context, helper_name, _forbidden(helper_name))

    bound = bind_launch_context(
        prepared=prepared,
        bindings=RuntimeBindings(
            spawn_id="p-bind",
            report_output_path=tmp_path / "report.md",
        ),
        runtime=_build_runtime(
            tmp_path=tmp_path,
            composition_surface=LaunchCompositionSurface.DIRECT,
        ),
        project_root=tmp_path,
        harness_registry=registry,
    )

    assert bound.run_params.continue_harness_session_id == "seed-session"
    assert bound.resolved_request.prompt == "hello"


@pytest.mark.parametrize(
    "surface",
    [
        LaunchCompositionSurface.PRIMARY,
        LaunchCompositionSurface.SPAWN_PREPARE,
    ],
)
def test_build_launch_context_prepare_surfaces_delegate_through_prepare_and_bind(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    surface: LaunchCompositionSurface,
) -> None:
    calls: list[str] = []
    prepared = SimpleNamespace(tag="prepared")
    bound = SimpleNamespace(tag="bound")

    def fake_prepare_launch_surface(**kwargs: object) -> object:
        assert isinstance(kwargs["catalog"], CatalogSession)
        calls.append("prepare")
        return prepared

    def fake_bind_launch_context(**kwargs: object) -> object:
        assert kwargs["prepared"] is prepared
        calls.append("bind")
        return bound

    monkeypatch.setattr(launch_context, "prepare_launch_surface", fake_prepare_launch_surface)
    monkeypatch.setattr(launch_context, "bind_launch_context", fake_bind_launch_context)

    result = build_launch_context(
        spawn_id="p-wrapper",
        request=_build_request(),
        runtime=_build_runtime(tmp_path=tmp_path, composition_surface=surface),
        harness_registry=get_default_harness_registry(),
        dry_run=True,
    )

    assert result is bound
    assert calls == ["prepare", "bind"]


def test_build_launch_context_direct_skips_prepare_and_uses_lightweight_surface(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    seen: dict[str, object] = {}

    monkeypatch.setattr(
        launch_context,
        "prepare_launch_surface",
        _forbidden("prepare_launch_surface"),
    )

    def fake_bind_launch_context(**kwargs: object) -> object:
        seen["prepared"] = kwargs["prepared"]
        return SimpleNamespace(tag="bound")

    monkeypatch.setattr(launch_context, "bind_launch_context", fake_bind_launch_context)

    result = build_launch_context(
        spawn_id="p-direct",
        request=_build_request(prompt_is_composed=True),
        runtime=_build_runtime(
            tmp_path=tmp_path,
            composition_surface=LaunchCompositionSurface.DIRECT,
        ),
        harness_registry=get_default_harness_registry(),
        dry_run=True,
    )

    prepared = seen["prepared"]
    assert isinstance(prepared, PreparedLaunchSurface)
    assert prepared.alias_catalog is None
    assert prepared.agent_inventory_prompt is None
    assert prepared.context_prompt is None
    assert result.tag == "bound"


def test_bind_launch_context_prefers_forked_runtime_session_without_mutating_prepared(
    tmp_path: Path,
) -> None:
    registry = get_default_harness_registry()
    prepared = PreparedLaunchSurface(
        request=_build_request(prompt_is_composed=True),
        harness=registry.get_subprocess_harness(HarnessId.CODEX),
        seed_harness_session_id="seed-session",
        composition_warnings=(),
        loaded_references=(),
        profile_tools_for_deny_optout=(),
        has_profile_for_deny_optout=False,
        projected_content=None,
        model_selection=None,
        alias_catalog=None,
        agent_inventory_prompt=None,
        context_prompt=None,
        seed_session_args=(),
        launch_request=None,
    )

    bound = bind_launch_context(
        prepared=prepared,
        bindings=RuntimeBindings(
            spawn_id="p-forked",
            report_output_path=tmp_path / "report.md",
            forked_harness_session_id="forked-session",
        ),
        runtime=_build_runtime(
            tmp_path=tmp_path,
            composition_surface=LaunchCompositionSurface.DIRECT,
        ),
        project_root=tmp_path,
        harness_registry=registry,
    )

    assert bound.run_params.continue_harness_session_id == "forked-session"
    assert prepared.seed_harness_session_id == "seed-session"
