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
    PreparedLaunchContent,
    PreparedLaunchSurface,
    PreparedPolicySurface,
    PreparedPromptPayload,
    RuntimeBindings,
    bind_launch_context,
    build_launch_context,
    compile_prepared_policy_surface,
    prepare_launch_surface,
)
from meridian.lib.launch.launch_types import PreparedLaunchRuntimeSeeds
from meridian.lib.launch.request import (
    LaunchArgvIntent,
    LaunchCompositionSurface,
    LaunchRuntime,
    RequestPromptPayload,
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


def _compile_policy_surface(
    *,
    tmp_path: Path,
    runtime: LaunchRuntime,
    request: SpawnRequest,
    catalog: CatalogSession,
    explicit_work_id: str | None = None,
) -> PreparedPolicySurface:
    return compile_prepared_policy_surface(
        request=request,
        runtime=runtime,
        project_root=tmp_path,
        harness_registry=get_default_harness_registry(),
        catalog=catalog,
        explicit_work_id=explicit_work_id,
        dry_run=True,
    )


def test_compile_prepared_policy_surface_does_not_call_projector_helpers(
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
        "load_reference_items",
        "build_launch_context_documents",
        "compose_skill_prompt_documents",
        "normalize_system_prompt_passthrough_args",
    ):
        monkeypatch.setattr(launch_context, helper_name, _forbidden(helper_name))

    prepared_policy = _compile_policy_surface(
        tmp_path=tmp_path,
        request=_build_request(),
        runtime=_build_runtime(
            tmp_path=tmp_path,
            composition_surface=LaunchCompositionSurface.SPAWN_PREPARE,
        ),
        catalog=catalog,
    )

    assert prepared_policy.resolved_policy.model == "gpt-5.4"
    assert prepared_policy.resolved_policy.harness == HarnessId.CODEX


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

    runtime = _build_runtime(
        tmp_path=tmp_path,
        composition_surface=LaunchCompositionSurface.SPAWN_PREPARE,
    )
    prepared_policy = _compile_policy_surface(
        tmp_path=tmp_path,
        runtime=runtime,
        request=_build_request(),
        catalog=catalog,
    )

    for helper_name in (
        "resolve_launch_policy",
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
        runtime=runtime,
        prepared_policy=prepared_policy,
    )

    assert prepared.request.prompt_is_composed is True
    assert prepared.request.harness == HarnessId.CODEX.value
    assert prepared.prompt_payload.user_turn_content == "hello"
    assert prepared.prompt_payload.appended_system_prompt is not None
    assert "adhoc_agent_payload" not in prepared.request.agent_metadata
    assert "appended_system_prompt" not in prepared.request.agent_metadata
    assert "user_turn_content" not in prepared.request.agent_metadata


def test_prepare_launch_surface_primary_does_not_use_spawn_prompt_policy_fallback(
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

    runtime = _build_runtime(
        tmp_path=tmp_path,
        composition_surface=LaunchCompositionSurface.PRIMARY,
    )
    prepared_policy = _compile_policy_surface(
        tmp_path=tmp_path,
        runtime=runtime,
        request=_build_request(),
        catalog=catalog,
    )
    monkeypatch.setattr(
        prepared_policy.resolved_policy.adapter,
        "run_prompt_policy",
        _forbidden("run_prompt_policy"),
    )

    prepared = prepare_launch_surface(
        request=_build_request(),
        runtime=runtime,
        prepared_policy=prepared_policy,
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
        composition_warnings=(),
        content=PreparedLaunchContent(prompt_payload=PreparedPromptPayload()),
        runtime_seeds=PreparedLaunchRuntimeSeeds(seed_harness_session_id="seed-session"),
        profile_tools_for_deny_optout=(),
        has_profile_for_deny_optout=False,
        model_selection=None,
        alias_catalog=None,
        launch_request=None,
    )
    for helper_name in (
        "load_reference_items",
        "resolve_launch_policy",
        "build_launch_context_documents",
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


def test_bind_launch_context_consumes_typed_prompt_payload(
    tmp_path: Path,
) -> None:
    registry = get_default_harness_registry()
    prepared = PreparedLaunchSurface(
        request=_build_request(prompt_is_composed=True).model_copy(
            update={
                "prompt_payload": RequestPromptPayload(
                    adhoc_agent_payload="wrong-adhoc",
                    appended_system_prompt="wrong-system",
                    user_turn_content="wrong-user",
                )
            }
        ),
        harness=registry.get_subprocess_harness(HarnessId.CODEX),
        composition_warnings=(),
        content=PreparedLaunchContent(
            prompt_payload=PreparedPromptPayload(
                adhoc_agent_payload="typed-adhoc",
                appended_system_prompt="typed-system",
                user_turn_content="typed-user",
            )
        ),
        runtime_seeds=PreparedLaunchRuntimeSeeds(),
        profile_tools_for_deny_optout=(),
        has_profile_for_deny_optout=False,
        model_selection=None,
        alias_catalog=None,
        launch_request=None,
    )

    bound = bind_launch_context(
        prepared=prepared,
        bindings=RuntimeBindings(
            spawn_id="p-typed-payload",
            report_output_path=tmp_path / "report.md",
        ),
        runtime=_build_runtime(
            tmp_path=tmp_path,
            composition_surface=LaunchCompositionSurface.DIRECT,
        ),
        project_root=tmp_path,
        harness_registry=registry,
    )

    assert bound.run_params.adhoc_agent_payload == "typed-adhoc"
    assert bound.run_params.appended_system_prompt == "typed-system"
    assert bound.run_params.user_turn_content == "typed-user"


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
    prepared_policy = SimpleNamespace(tag="policy")
    prepared = SimpleNamespace(tag="prepared")
    bound = SimpleNamespace(tag="bound")

    def fake_compile_prepared_policy_surface(**kwargs: object) -> object:
        assert isinstance(kwargs["catalog"], CatalogSession)
        calls.append("compile")
        return prepared_policy

    def fake_prepare_launch_surface(**kwargs: object) -> object:
        assert kwargs["prepared_policy"] is prepared_policy
        calls.append("prepare")
        return prepared

    def fake_bind_launch_context(**kwargs: object) -> object:
        assert kwargs["prepared"] is prepared
        calls.append("bind")
        return bound

    monkeypatch.setattr(
        launch_context,
        "compile_prepared_policy_surface",
        fake_compile_prepared_policy_surface,
    )
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
    assert calls == ["compile", "prepare", "bind"]


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
    monkeypatch.setattr(
        launch_context,
        "compile_prepared_policy_surface",
        _forbidden("compile_prepared_policy_surface"),
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
        composition_warnings=(),
        content=PreparedLaunchContent(prompt_payload=PreparedPromptPayload()),
        runtime_seeds=PreparedLaunchRuntimeSeeds(seed_harness_session_id="seed-session"),
        profile_tools_for_deny_optout=(),
        has_profile_for_deny_optout=False,
        model_selection=None,
        alias_catalog=None,
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


def test_build_launch_context_uses_explicit_request_work_for_prepare_and_bind(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _write_minimal_mars_config(tmp_path)
    monkeypatch.delenv("MERIDIAN_ACTIVE_WORK_ID", raising=False)
    monkeypatch.delenv("MERIDIAN_ACTIVE_WORK_DIR", raising=False)

    captured_active_work_dirs: list[Path | None] = []
    def fake_build_launch_context_documents(**kwargs: object) -> tuple[str, str]:
        captured_active_work_dirs.append(kwargs["active_work_dir"])
        return ("", "")

    monkeypatch.setattr(
        launch_context,
        "build_launch_context_documents",
        fake_build_launch_context_documents,
    )

    request = _build_request(prompt_is_composed=False).model_copy(
        update={"work_id_hint": "work-explicit"}
    )
    runtime = _build_runtime(
        tmp_path=tmp_path,
        composition_surface=LaunchCompositionSurface.SPAWN_PREPARE,
    )

    bound = build_launch_context(
        spawn_id="p-explicit-work",
        request=request,
        runtime=runtime,
        harness_registry=get_default_harness_registry(),
        dry_run=True,
    )

    expected_work_dir = tmp_path / ".meridian" / "work" / "work-explicit"
    assert captured_active_work_dirs == [expected_work_dir]
    assert bound.work_id == "work-explicit"
    assert bound.env_overrides["MERIDIAN_ACTIVE_WORK_ID"] == "work-explicit"
    assert bound.env_overrides["MERIDIAN_ACTIVE_WORK_DIR"] == expected_work_dir.as_posix()


def test_build_launch_context_runtime_work_overrides_inherited_work_for_prepare_and_bind(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _write_minimal_mars_config(tmp_path)
    monkeypatch.setenv("MERIDIAN_ACTIVE_WORK_ID", "work-inherited")
    monkeypatch.setenv(
        "MERIDIAN_ACTIVE_WORK_DIR",
        (tmp_path / ".meridian" / "work" / "work-inherited").as_posix(),
    )

    captured_active_work_dirs: list[Path | None] = []
    def fake_build_launch_context_documents(**kwargs: object) -> tuple[str, str]:
        captured_active_work_dirs.append(kwargs["active_work_dir"])
        return ("", "")

    monkeypatch.setattr(
        launch_context,
        "build_launch_context_documents",
        fake_build_launch_context_documents,
    )

    request = _build_request(prompt_is_composed=False).model_copy(
        update={"work_id_hint": "work-request"}
    )
    runtime = _build_runtime(
        tmp_path=tmp_path,
        composition_surface=LaunchCompositionSurface.SPAWN_PREPARE,
    )

    bound = build_launch_context(
        spawn_id="p-runtime-work",
        request=request,
        runtime=runtime,
        harness_registry=get_default_harness_registry(),
        runtime_work_id="work-runtime",
        dry_run=True,
    )

    expected_work_dir = tmp_path / ".meridian" / "work" / "work-runtime"
    assert captured_active_work_dirs == [expected_work_dir]
    assert bound.work_id == "work-runtime"
    assert bound.env_overrides["MERIDIAN_ACTIVE_WORK_ID"] == "work-runtime"
    assert bound.env_overrides["MERIDIAN_ACTIVE_WORK_DIR"] == expected_work_dir.as_posix()
