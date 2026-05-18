# qa-validated: orchestrator-opencode-fallback-runtime
# qa-validated: mars-launch-bundle-design
import json
import subprocess
from pathlib import Path

import pytest

from meridian.lib.catalog.agent import load_agent_profile
from meridian.lib.catalog.catalog_session import CatalogSession
from meridian.lib.catalog.model_aliases import AliasEntry
from meridian.lib.config.settings import MeridianConfig
from meridian.lib.core.overrides import RuntimeOverrides
from meridian.lib.core.types import HarnessId, ModelId
from meridian.lib.harness.registry import HarnessRegistry, get_default_harness_registry
from meridian.lib.launch.mars_bundle import (
    BundleExecutionPolicy,
    BundlePromptSurface,
    BundleRouting,
    BundleScaffoldSlots,
    BundleSkillsMetadata,
    BundleTools,
    LaunchBundle,
    MarsLaunchBundleError,
    MarsLaunchBundleUnavailableError,
    build_launch_bundle_command,
    invoke_mars_build_launch_bundle,
    parse_launch_bundle,
)
from meridian.lib.launch.policies import (
    ModelSelectionContext,
    SurfacePolicyInput,
    match_model_policy,
    resolve_launch_policy,
    resolve_policy_fields,
    validate_harness_compatibility,
)
from meridian.lib.launch.request import LaunchCompositionSurface


def _write_agent_profile(project_root: Path, *, name: str, frontmatter: str) -> None:
    path = project_root / ".mars" / "agents" / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{frontmatter}\n---\n\n# {name}\n", encoding="utf-8")


def _mock_alias(
    *,
    alias: str,
    model_id: str,
    harness: HarnessId = HarnessId.CODEX,
    default_effort: str | None = None,
) -> AliasEntry:
    return AliasEntry(
        alias=alias,
        model_id=ModelId(model_id),
        resolved_harness=harness,
        default_effort=default_effort,
    )


def _patch_alias_resolution(
    monkeypatch: pytest.MonkeyPatch,
    *,
    resolved_entries: dict[str, AliasEntry],
) -> None:
    def resolve_entry(self: CatalogSession, name: str) -> AliasEntry:
        _ = self
        try:
            return resolved_entries[name]
        except KeyError as exc:
            raise ValueError(f"Unknown model alias '{name}'") from exc

    def list_entries(self: CatalogSession) -> list[AliasEntry]:
        _ = self
        return list(resolved_entries.values())

    monkeypatch.setattr(CatalogSession, "resolve_model", resolve_entry)
    monkeypatch.setattr(CatalogSession, "load_aliases", list_entries)


def _registry_with_harnesses(*harness_ids: HarnessId) -> HarnessRegistry:
    base_registry = get_default_harness_registry()
    registry = HarnessRegistry()
    for harness_id in harness_ids:
        registry.register(base_registry.get(harness_id))
    return registry


@pytest.fixture(autouse=True)
def _default_bundle_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "meridian.lib.launch.policies.invoke_mars_build_launch_bundle",
        lambda **_: (
            _ for _ in ()
        ).throw(MarsLaunchBundleUnavailableError("mars unavailable in unit tests")),
    )


def _bundle_for_tests(
    *,
    model: str = "gpt-5.4",
    model_token: str | None = None,
    harness: str = "codex",
    harness_model: str | None = None,
    harness_model_source: str | None = None,
    harness_model_confidence: str | None = None,
    native_config: dict[str, object] | None = None,
) -> LaunchBundle:
    return LaunchBundle(
        version=1,
        agent="reviewer",
        routing=BundleRouting(
            model=model,
            model_token=model_token or model,
            harness=harness,
            harness_model=harness_model,
            harness_model_source=harness_model_source,
            harness_model_confidence=harness_model_confidence,
        ),
        execution_policy=BundleExecutionPolicy(
            effort="high",
            approval="auto",
            sandbox="workspace-write",
            native_config=native_config,
        ),
        prompt_surface=BundlePromptSurface(system_instruction="Bundle system instruction"),
        scaffold_slots=BundleScaffoldSlots(),
        tools=BundleTools(
            allowed=("Bash", "Read"),
            disallowed=("Write",),
            mcp=("github=gh",),
        ),
        skills_metadata=BundleSkillsMetadata(loaded=("verification",), missing=()),
        provenance={"model_source": "mars"},
        warnings=("bundle warning",),
    )


def _bundle_payload_with_slots(scaffold_slots: dict[str, object]) -> str:
    return json.dumps(
        {
            "version": 1,
            "agent": "reviewer",
            "routing": {
                "model": "gpt-5.4",
                "model_token": "gpt-5.4",
                "harness": "codex",
            },
            "execution_policy": {},
            "prompt_surface": {
                "system_instruction": "Rendered by Mars",
                "supplemental_documents": [
                    {
                        "kind": "skill",
                        "name": "verification",
                        "content": "doc content",
                        "skill_type": "reference",
                    }
                ],
                "inventory_prompt": "inventory text",
            },
            "scaffold_slots": scaffold_slots,
            "tools": {},
            "skills_metadata": {},
            "provenance": {},
            "warnings": [],
        }
    )


def test_build_launch_bundle_command_relays_effective_overrides_and_requested_skills(
    tmp_path: Path,
) -> None:
    command = build_launch_bundle_command(
        agent="reviewer",
        project_root=tmp_path,
        cli_overrides=RuntimeOverrides(
            model="gpt55",
            approval="auto",
            sandbox="workspace-write",
        ),
        env_overrides=RuntimeOverrides(
            model="env-model",
            harness="opencode",
            effort="medium",
            approval="confirm",
        ),
        requested_skills=("unit-test", "", "testing-principles"),
        executable="/usr/bin/mars",
    )

    assert command == (
        "/usr/bin/mars",
        "build",
        "launch-bundle",
        "--agent",
        "reviewer",
        "--root",
        tmp_path.as_posix(),
        "--json",
        "--model",
        "gpt55",
        "--harness",
        "opencode",
        "--effort",
        "medium",
        "--approval",
        "auto",
        "--sandbox",
        "workspace-write",
        "--skill",
        "unit-test",
        "--skill",
        "testing-principles",
    )


def test_parse_launch_bundle_accepts_placeholder_scaffold_slots() -> None:
    raw_json = _bundle_payload_with_slots(
        {
            "completion_contract": "###SLOT###",
            "context_prompt": "",
            "user_prompt_file": None,
            "extra_slot": "###SLOT###",
        }
    )

    bundle = parse_launch_bundle(raw_json)

    assert bundle.prompt_surface.system_instruction == "Rendered by Mars"
    assert bundle.prompt_surface.inventory_prompt == "inventory text"
    assert bundle.prompt_surface.supplemental_documents[0].name == "verification"


def test_parse_launch_bundle_preserves_native_config_and_mixed_tools() -> None:
    payload = json.loads(_bundle_payload_with_slots({}))
    payload["execution_policy"] = {
        "native_config": {
            "sandbox_workspace_write.network_access": True,
            "allowed_tools": ["Bash", "Read"],
        }
    }
    payload["tools"] = {
        "allowed": ["Bash", "Read"],
        "disallowed": ["Write"],
        "mcp": ["github=gh"],
    }

    bundle = parse_launch_bundle(json.dumps(payload))

    assert bundle.execution_policy.native_config == {
        "sandbox_workspace_write.network_access": True,
        "allowed_tools": ["Bash", "Read"],
    }
    assert bundle.tools.allowed == ("Bash", "Read")
    assert bundle.tools.disallowed == ("Write",)
    assert bundle.tools.mcp == ("github=gh",)
    assert bundle.tools.to_tools_field() == {
        "Bash": "allow",
        "Read": "allow",
        "Write": "deny",
    }


def test_parse_launch_bundle_preserves_routing_harness_model_fields() -> None:
    payload = json.loads(_bundle_payload_with_slots({}))
    payload["routing"] = {
        "model": "gpt-5.5",
        "model_token": "gpt55",
        "harness": "opencode",
        "harness_model": "openai/gpt-5.5",
        "harness_model_source": "candidate-path",
        "harness_model_confidence": "high",
    }

    bundle = parse_launch_bundle(json.dumps(payload))

    assert bundle.routing.model == "gpt-5.5"
    assert bundle.routing.model_token == "gpt55"
    assert bundle.routing.harness == "opencode"
    assert bundle.routing.harness_model == "openai/gpt-5.5"
    assert bundle.routing.harness_model_source == "candidate-path"
    assert bundle.routing.harness_model_confidence == "high"


def test_parse_launch_bundle_rejects_newer_schema_version() -> None:
    payload = json.loads(_bundle_payload_with_slots({}))
    payload["version"] = 2

    with pytest.raises(
        MarsLaunchBundleError,
        match="schema version 2 is newer than supported 1",
    ):
        parse_launch_bundle(json.dumps(payload))


def test_parse_launch_bundle_rejects_prefilled_known_scaffold_slot() -> None:
    raw_json = _bundle_payload_with_slots({"context_prompt": "already filled"})

    with pytest.raises(
        MarsLaunchBundleError,
        match=r"prefilled scaffold slot 'context_prompt'",
    ):
        parse_launch_bundle(raw_json)


def test_parse_launch_bundle_rejects_prefilled_extra_scaffold_slot() -> None:
    raw_json = _bundle_payload_with_slots({"future_slot": "already filled"})

    with pytest.raises(
        MarsLaunchBundleError,
        match=r"prefilled scaffold slot 'future_slot'",
    ):
        parse_launch_bundle(raw_json)


def test_invoke_launch_bundle_reclassifies_unsupported_command_as_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        "meridian.lib.launch.mars_bundle.resolve_mars_executable",
        lambda: "mars",
    )
    monkeypatch.setattr(
        "meridian.lib.launch.mars_bundle.subprocess.run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=["mars", "build", "launch-bundle"],
            returncode=2,
            stdout="",
            stderr="error: unrecognized subcommand 'launch-bundle'",
        ),
    )

    with pytest.raises(
        MarsLaunchBundleUnavailableError,
        match="does not support 'build launch-bundle'",
    ):
        invoke_mars_build_launch_bundle(
            agent="reviewer",
            project_root=tmp_path,
            cli_overrides=RuntimeOverrides(),
            env_overrides=RuntimeOverrides(),
        )


def test_invoke_launch_bundle_nonzero_ordinary_error_is_hard_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        "meridian.lib.launch.mars_bundle.resolve_mars_executable",
        lambda: "mars",
    )
    monkeypatch.setattr(
        "meridian.lib.launch.mars_bundle.subprocess.run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=["mars", "build", "launch-bundle"],
            returncode=1,
            stdout="",
            stderr="failed to resolve profile 'reviewer'",
        ),
    )

    with pytest.raises(MarsLaunchBundleError, match="failed to resolve profile"):
        invoke_mars_build_launch_bundle(
            agent="reviewer",
            project_root=tmp_path,
            cli_overrides=RuntimeOverrides(),
            env_overrides=RuntimeOverrides(),
        )


def test_model_selection_context_has_harness_model_id_field() -> None:
    context = ModelSelectionContext(
        requested_token="fast",
        selected_model_token="fast",
        canonical_model_id="fake-model",
        mars_provided_harness=HarnessId.CODEX,
        resolved_entry=None,
        harness_provenance="resolved",
    )

    assert hasattr(context, "harness_model_id")


def test_model_selection_context_harness_model_id_defaults_to_none() -> None:
    context = ModelSelectionContext(
        requested_token="fast",
        selected_model_token="fast",
        canonical_model_id="fake-model",
        mars_provided_harness=HarnessId.CODEX,
        resolved_entry=None,
        harness_provenance="resolved",
    )

    assert context.harness_model_id is None


def test_resolve_launch_policy_spawn_prepare_agent_bundle_path_skips_profile_loading(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    bundle = _bundle_for_tests(
        model="gpt-5.3-codex",
        harness="codex",
        native_config={"sandbox_workspace_write.network_access": True},
    )

    def _fake_bundle(**kwargs: object) -> LaunchBundle:
        assert kwargs["agent"] == "reviewer"
        return bundle

    def _compile_should_not_run(*_: object, **__: object) -> object:
        raise AssertionError("legacy compiler path should not run for bundle launch")

    def _load_should_not_run(*_: object, **__: object) -> object:
        raise AssertionError("profile loading should not run for bundle launch")

    monkeypatch.setattr(
        "meridian.lib.launch.policies.invoke_mars_build_launch_bundle",
        _fake_bundle,
    )
    monkeypatch.setattr(
        "meridian.lib.launch.policies.compile_launch_params",
        _compile_should_not_run,
    )
    monkeypatch.setattr(
        "meridian.lib.launch.policies.load_agent_profile_with_fallback",
        _load_should_not_run,
    )

    policy = resolve_launch_policy(
        SurfacePolicyInput(
            surface=LaunchCompositionSurface.SPAWN_PREPARE,
            catalog=CatalogSession(tmp_path),
            layers=(RuntimeOverrides(agent="reviewer"), RuntimeOverrides()),
            config_overrides=RuntimeOverrides.from_config(MeridianConfig()),
            config=MeridianConfig(),
            harness_registry=get_default_harness_registry(),
        )
    )

    assert policy.launch_bundle is not None
    assert policy.profile is None
    assert policy.model == "gpt-5.3-codex"
    assert policy.harness == HarnessId.CODEX
    assert policy.routing.agent == "reviewer"
    assert policy.execution_policy.approval == "auto"
    assert policy.tools is None
    assert policy.mcp_tools == ("github=gh",)
    assert policy.bundle_extra_args == ("-c", "sandbox_workspace_write.network_access=true")
    assert any(
        "Tool-level allow/deny policy is not projected for Codex" in w.message
        for w in policy.warnings
    )


def test_resolve_launch_policy_bundle_unavailable_falls_back_to_legacy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _write_agent_profile(
        tmp_path,
        name="reviewer",
        frontmatter="name: reviewer\nmodel: codex\n",
    )
    alias = _mock_alias(alias="codex", model_id="gpt-5.3-codex", harness=HarnessId.CODEX)
    _patch_alias_resolution(
        monkeypatch,
        resolved_entries={"codex": alias, "gpt-5.3-codex": alias},
    )

    def _raise_bundle_error(**_: object) -> LaunchBundle:
        raise MarsLaunchBundleUnavailableError("old mars")

    monkeypatch.setattr(
        "meridian.lib.launch.policies.invoke_mars_build_launch_bundle",
        _raise_bundle_error,
    )

    policy = resolve_launch_policy(
        SurfacePolicyInput(
            surface=LaunchCompositionSurface.SPAWN_PREPARE,
            catalog=CatalogSession(tmp_path),
            layers=(RuntimeOverrides(agent="reviewer"), RuntimeOverrides()),
            config_overrides=RuntimeOverrides.from_config(MeridianConfig()),
            config=MeridianConfig(),
            harness_registry=get_default_harness_registry(),
        )
    )

    assert policy.launch_bundle is None
    assert policy.model == "gpt-5.3-codex"
    assert any("falling back to legacy launch resolution" in w.message for w in policy.warnings)


def test_resolve_launch_policy_bundle_error_raises_without_legacy_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def _raise_bundle_error(**_: object) -> LaunchBundle:
        raise MarsLaunchBundleError("bundle build failed")

    def _load_should_not_run(*_: object, **__: object) -> object:
        raise AssertionError("legacy fallback should not run for bundle build errors")

    monkeypatch.setattr(
        "meridian.lib.launch.policies.invoke_mars_build_launch_bundle",
        _raise_bundle_error,
    )
    monkeypatch.setattr(
        "meridian.lib.launch.policies.load_agent_profile_with_fallback",
        _load_should_not_run,
    )

    with pytest.raises(MarsLaunchBundleError, match="bundle build failed"):
        resolve_launch_policy(
            SurfacePolicyInput(
                surface=LaunchCompositionSurface.SPAWN_PREPARE,
                catalog=CatalogSession(tmp_path),
                layers=(RuntimeOverrides(agent="reviewer"), RuntimeOverrides()),
                config_overrides=RuntimeOverrides.from_config(MeridianConfig()),
                config=MeridianConfig(),
                harness_registry=get_default_harness_registry(),
            )
        )


def test_resolve_launch_policy_bundle_unsupported_harness_raises_clear_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        "meridian.lib.launch.policies.invoke_mars_build_launch_bundle",
        lambda **_: _bundle_for_tests(harness="not-a-harness"),
    )

    with pytest.raises(ValueError, match="unsupported harness 'not-a-harness'"):
        resolve_launch_policy(
            SurfacePolicyInput(
                surface=LaunchCompositionSurface.SPAWN_PREPARE,
                catalog=CatalogSession(tmp_path),
                layers=(RuntimeOverrides(agent="reviewer"), RuntimeOverrides()),
                config_overrides=RuntimeOverrides.from_config(MeridianConfig()),
                config=MeridianConfig(),
                harness_registry=get_default_harness_registry(),
            )
        )


def test_resolve_launch_policy_bundle_non_codex_native_config_warns_and_skips_projection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        "meridian.lib.launch.policies.invoke_mars_build_launch_bundle",
        lambda **_: _bundle_for_tests(
            harness="claude",
            model="claude-sonnet-4-5",
            native_config={"sandbox_workspace_write.network_access": True},
        ),
    )

    policy = resolve_launch_policy(
        SurfacePolicyInput(
            surface=LaunchCompositionSurface.SPAWN_PREPARE,
            catalog=CatalogSession(tmp_path),
            layers=(RuntimeOverrides(agent="reviewer"), RuntimeOverrides()),
            config_overrides=RuntimeOverrides.from_config(MeridianConfig()),
            config=MeridianConfig(),
            harness_registry=get_default_harness_registry(),
        )
    )

    assert policy.harness == HarnessId.CLAUDE
    assert policy.bundle_extra_args == ()
    assert any(
        "native_config is not projected for harness 'claude'" in warning.message
        for warning in policy.warnings
    )


def test_resolve_launch_policy_bundle_non_codex_keeps_tools_projection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        "meridian.lib.launch.policies.invoke_mars_build_launch_bundle",
        lambda **_: _bundle_for_tests(
            harness="claude",
            model="claude-sonnet-4-5",
            native_config=None,
        ),
    )

    policy = resolve_launch_policy(
        SurfacePolicyInput(
            surface=LaunchCompositionSurface.SPAWN_PREPARE,
            catalog=CatalogSession(tmp_path),
            layers=(RuntimeOverrides(agent="reviewer"), RuntimeOverrides()),
            config_overrides=RuntimeOverrides.from_config(MeridianConfig()),
            config=MeridianConfig(),
            harness_registry=get_default_harness_registry(),
        )
    )

    assert policy.harness == HarnessId.CLAUDE
    assert policy.tools == {"Bash": "allow", "Read": "allow", "Write": "deny"}


def test_resolve_launch_policy_bundle_preserves_mars_tool_keys_without_normalizing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        "meridian.lib.launch.policies.invoke_mars_build_launch_bundle",
        lambda **_: _bundle_for_tests(
            harness="claude",
            model="claude-sonnet-4-5",
        ).model_copy(
            update={
                "tools": BundleTools(
                    allowed=("Bash(grep -R)", "mcp__github__create_issue"),
                    disallowed=("Write(File)",),
                )
            }
        ),
    )

    policy = resolve_launch_policy(
        SurfacePolicyInput(
            surface=LaunchCompositionSurface.SPAWN_PREPARE,
            catalog=CatalogSession(tmp_path),
            layers=(RuntimeOverrides(agent="reviewer"), RuntimeOverrides()),
            config_overrides=RuntimeOverrides.from_config(MeridianConfig()),
            config=MeridianConfig(),
            harness_registry=get_default_harness_registry(),
        )
    )

    assert policy.tools == {
        "Bash(grep -R)": "allow",
        "mcp__github__create_issue": "allow",
        "Write(File)": "deny",
    }


def test_resolve_launch_policy_bundle_overlays_explicit_execution_policy_fields(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        "meridian.lib.launch.policies.invoke_mars_build_launch_bundle",
        lambda **_: _bundle_for_tests(),
    )

    policy = resolve_launch_policy(
        SurfacePolicyInput(
            surface=LaunchCompositionSurface.SPAWN_PREPARE,
            catalog=CatalogSession(tmp_path),
            layers=(
                RuntimeOverrides(
                    agent="reviewer",
                    approval="confirm",
                    autocompact=9000,
                    timeout=18.5,
                ),
                RuntimeOverrides(
                    effort="low",
                    sandbox="read-only",
                    approval="auto",
                    autocompact_pct=33,
                    timeout=60.0,
                ),
            ),
            config_overrides=RuntimeOverrides.from_config(MeridianConfig()),
            config=MeridianConfig(),
            harness_registry=get_default_harness_registry(),
        )
    )

    assert policy.execution_policy.effort == "low"
    assert policy.execution_policy.sandbox == "read-only"
    assert policy.execution_policy.approval == "confirm"
    assert policy.execution_policy.autocompact == 9000
    assert policy.execution_policy.autocompact_pct == 33
    assert policy.execution_policy.timeout == 18.5


def test_resolve_launch_policy_bundle_execution_policy_uses_config_defaults_when_unset(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        "meridian.lib.launch.policies.invoke_mars_build_launch_bundle",
        lambda **_: _bundle_for_tests().model_copy(
            update={
                "execution_policy": BundleExecutionPolicy(),
            }
        ),
    )
    config = MeridianConfig.model_validate(
        {
            "primary": {
                "effort": "high",
                "sandbox": "read-only",
                "approval": "confirm",
                "autocompact_pct": 41,
                "timeout": 75.0,
            }
        }
    )

    policy = resolve_launch_policy(
        SurfacePolicyInput(
            surface=LaunchCompositionSurface.SPAWN_PREPARE,
            catalog=CatalogSession(tmp_path),
            layers=(
                RuntimeOverrides(
                    agent="reviewer",
                    approval="auto",
                ),
                RuntimeOverrides(
                    effort="low",
                    approval="confirm",
                ),
            ),
            config_overrides=RuntimeOverrides.from_config(config),
            config=config,
            harness_registry=get_default_harness_registry(),
        )
    )

    assert policy.execution_policy.effort == "low"
    assert policy.execution_policy.sandbox == "read-only"
    assert policy.execution_policy.approval == "auto"
    assert policy.execution_policy.autocompact_pct == 41
    assert policy.execution_policy.timeout == 75.0


def test_resolve_launch_policy_bundle_execution_policy_overrides_config_when_set(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        "meridian.lib.launch.policies.invoke_mars_build_launch_bundle",
        lambda **_: _bundle_for_tests().model_copy(
            update={
                "execution_policy": BundleExecutionPolicy(
                    effort="medium",
                    sandbox="workspace-write",
                    approval="auto",
                    autocompact=62000,
                    autocompact_pct=52,
                    timeout=42.0,
                ),
            }
        ),
    )
    config = MeridianConfig.model_validate(
        {
            "primary": {
                "effort": "high",
                "sandbox": "read-only",
                "approval": "confirm",
                "autocompact": 18000,
                "autocompact_pct": 11,
                "timeout": 9.5,
            }
        }
    )

    policy = resolve_launch_policy(
        SurfacePolicyInput(
            surface=LaunchCompositionSurface.SPAWN_PREPARE,
            catalog=CatalogSession(tmp_path),
            layers=(RuntimeOverrides(agent="reviewer"), RuntimeOverrides()),
            config_overrides=RuntimeOverrides.from_config(config),
            config=config,
            harness_registry=get_default_harness_registry(),
        )
    )

    assert policy.execution_policy.effort == "medium"
    assert policy.execution_policy.sandbox == "workspace-write"
    assert policy.execution_policy.approval == "auto"
    assert policy.execution_policy.autocompact == 62000
    assert policy.execution_policy.autocompact_pct == 52
    assert policy.execution_policy.timeout == 42.0


def test_resolve_launch_policy_bundle_uses_harness_model_for_adapter_id_only(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        "meridian.lib.launch.policies.invoke_mars_build_launch_bundle",
        lambda **_: _bundle_for_tests(
            model="gpt-5.5",
            model_token="gpt55",
            harness="opencode",
            harness_model=" openai/gpt-5.5 ",
            harness_model_source=" candidate-path ",
            harness_model_confidence=" high ",
        ),
    )

    policy = resolve_launch_policy(
        SurfacePolicyInput(
            surface=LaunchCompositionSurface.SPAWN_PREPARE,
            catalog=CatalogSession(tmp_path),
            layers=(RuntimeOverrides(agent="reviewer"), RuntimeOverrides()),
            config_overrides=RuntimeOverrides.from_config(MeridianConfig()),
            config=MeridianConfig(),
            harness_registry=get_default_harness_registry(),
        )
    )

    assert policy.model == "gpt-5.5"
    assert policy.routing.model == "gpt55"
    assert policy.model_selection is not None
    assert policy.model_selection.selected_model_token == "gpt55"
    assert policy.model_selection.canonical_model_id == "gpt-5.5"
    assert policy.model_selection.harness_model_id == "openai/gpt-5.5"
    assert policy.model_selection.harness_model_source == "candidate-path"
    assert policy.model_selection.harness_model_confidence == "high"


def test_resolve_launch_policy_bundle_native_config_flags_are_sorted(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        "meridian.lib.launch.policies.invoke_mars_build_launch_bundle",
        lambda **_: _bundle_for_tests(
            native_config={
                "z.last": 1,
                "a.first": True,
            }
        ),
    )

    policy = resolve_launch_policy(
        SurfacePolicyInput(
            surface=LaunchCompositionSurface.SPAWN_PREPARE,
            catalog=CatalogSession(tmp_path),
            layers=(RuntimeOverrides(agent="reviewer"), RuntimeOverrides()),
            config_overrides=RuntimeOverrides.from_config(MeridianConfig()),
            config=MeridianConfig(),
            harness_registry=get_default_harness_registry(),
        )
    )

    assert policy.bundle_extra_args == (
        "-c",
        "a.first=true",
        "-c",
        "z.last=1",
    )


def test_resolve_policy_fields_resolves_per_field_precedence() -> None:
    resolved = resolve_policy_fields(
        RuntimeOverrides(timeout=12.5),
        RuntimeOverrides(sandbox="workspace-write"),
        RuntimeOverrides(effort="medium", sandbox="read-only"),
        RuntimeOverrides(approval="confirm", autocompact=70000),
        RuntimeOverrides(effort="low", approval="auto", autocompact=30000),
    )

    assert resolved.timeout == 12.5
    assert resolved.sandbox == "workspace-write"
    assert resolved.effort == "medium"
    assert resolved.approval == "confirm"
    assert resolved.autocompact == 70000


def test_resolve_policy_fields_model_policy_scope_strips_routing_fields() -> None:
    resolved = resolve_policy_fields(
        RuntimeOverrides(
            model="gpt55",
            harness="codex",
            agent="reviewer",
            effort="high",
        ).model_policy_scope(),
        RuntimeOverrides(
            model="claude",
            harness="claude",
            agent="fallback",
            approval="auto",
        ).model_policy_scope(),
    )

    assert resolved.model is None
    assert resolved.harness is None
    assert resolved.agent is None
    assert resolved.effort == "high"
    assert resolved.approval == "auto"


def test_chat_and_spawn_prepare_surfaces_resolve_equivalent_shared_policy_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    alias = _mock_alias(alias="codex", model_id="gpt-5.3-codex", harness=HarnessId.CODEX)
    _patch_alias_resolution(
        monkeypatch,
        resolved_entries={"codex": alias, "gpt-5.3-codex": alias},
    )
    registry = get_default_harness_registry()
    layers = (
        RuntimeOverrides(model="codex", approval="auto", sandbox="workspace-write", effort="high"),
        RuntimeOverrides(),
    )
    config = MeridianConfig()

    chat_policy = resolve_launch_policy(
        SurfacePolicyInput(
            surface=LaunchCompositionSurface.CHAT,
            catalog=CatalogSession(Path.cwd()),
            layers=layers,
            config_overrides=RuntimeOverrides.from_config(config),
            config=config,
            harness_registry=registry,
            supported_execution_policy_fields=frozenset(
                {"effort", "sandbox", "approval", "autocompact"}
            ),
        )
    )
    spawn_policy = resolve_launch_policy(
        SurfacePolicyInput(
            surface=LaunchCompositionSurface.SPAWN_PREPARE,
            catalog=CatalogSession(Path.cwd()),
            layers=layers,
            config_overrides=RuntimeOverrides.from_config(config),
            config=config,
            harness_registry=registry,
        )
    )

    assert chat_policy.model == spawn_policy.model == "gpt-5.3-codex"
    assert chat_policy.harness == spawn_policy.harness == HarnessId.CODEX
    assert chat_policy.execution_policy.effort == spawn_policy.execution_policy.effort == "high"
    assert chat_policy.execution_policy.sandbox == spawn_policy.execution_policy.sandbox
    assert chat_policy.execution_policy.approval == spawn_policy.execution_policy.approval


def test_match_model_policy_first_match_wins_by_list_order(tmp_path: Path) -> None:
    _write_agent_profile(
        tmp_path,
        name="reviewer",
        frontmatter=(
            "name: reviewer\n"
            "model-policies:\n"
            "  - match: {model-glob: 'gpt-*'}\n"
            "    override: {effort: low}\n"
            "  - match: {alias: fast}\n"
            "    override: {effort: medium}\n"
            "  - match: {model: gpt-5.5}\n"
            "    override: {effort: high}\n"
        ),
    )
    profile = load_agent_profile("reviewer", tmp_path)

    # canonical_id matches glob AND alias matches alias rule AND model matches model rule —
    # first rule (glob) must win
    winner = match_model_policy(
        model_policies=profile.model_policies,
        canonical_model_id="gpt-5.5",
        selected_model_token="fast",
    )

    assert winner is not None
    assert winner.match_type == "model-glob"
    assert winner.match_value == "gpt-*"


def test_validate_harness_compatibility_allows_model_policy_candidate_route() -> None:
    registry = get_default_harness_registry()
    model_entry = _mock_alias(
        alias="claude",
        model_id="claude-haiku-4-5",
        harness=HarnessId.CLAUDE,
    )
    object.__setattr__(model_entry, "harness_candidates", ("claude", "codex"))

    validate_harness_compatibility(
        model="claude-haiku-4-5",
        harness_id=HarnessId.CODEX,
        model_entry=model_entry,
        harness_registry=registry,
    )


def test_validate_harness_compatibility_allows_same_layer_contradiction() -> None:
    registry = get_default_harness_registry()
    model_entry = _mock_alias(
        alias="claude",
        model_id="claude-haiku-4-5",
        harness=HarnessId.CLAUDE,
    )

    validate_harness_compatibility(
        model="claude-haiku-4-5",
        harness_id=HarnessId.CODEX,
        model_entry=model_entry,
        harness_registry=registry,
    )


def test_resolve_launch_policy_primary_allows_model_derived_pi_harness(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    pi_alias = _mock_alias(alias="pi-fast", model_id="pi-fast-model", harness=HarnessId.PI)
    _patch_alias_resolution(
        monkeypatch,
        resolved_entries={
            "pi-fast": pi_alias,
            "pi-fast-model": pi_alias,
        },
    )

    policy = resolve_launch_policy(
        SurfacePolicyInput(
            surface=LaunchCompositionSurface.PRIMARY,
            catalog=CatalogSession(tmp_path),
            layers=(RuntimeOverrides(model="pi-fast"), RuntimeOverrides()),
            config_overrides=RuntimeOverrides.from_config(MeridianConfig()),
            config=MeridianConfig(),
            harness_registry=_registry_with_harnesses(HarnessId.PI, HarnessId.CODEX),
        )
    )

    assert policy.harness == HarnessId.PI


def test_resolve_launch_policy_primary_allows_config_default_pi_harness(
    tmp_path: Path,
) -> None:
    config = MeridianConfig.model_validate({"primary": {"harness": "pi"}})

    policy = resolve_launch_policy(
        SurfacePolicyInput(
            surface=LaunchCompositionSurface.PRIMARY,
            catalog=CatalogSession(tmp_path),
            layers=(RuntimeOverrides(), RuntimeOverrides()),
            config_overrides=RuntimeOverrides.from_config(config),
            config=config,
            harness_registry=_registry_with_harnesses(HarnessId.PI, HarnessId.CODEX),
        )
    )

    assert policy.harness == HarnessId.PI


def test_resolve_launch_policy_spawn_prepare_allows_pi_harness(tmp_path: Path) -> None:
    policy = resolve_launch_policy(
        SurfacePolicyInput(
            surface=LaunchCompositionSurface.SPAWN_PREPARE,
            catalog=CatalogSession(tmp_path),
            layers=(RuntimeOverrides(harness="pi"), RuntimeOverrides()),
            config_overrides=RuntimeOverrides.from_config(MeridianConfig()),
            config=MeridianConfig(),
            harness_registry=_registry_with_harnesses(HarnessId.PI, HarnessId.CODEX),
        )
    )

    assert policy.harness == HarnessId.PI


def test_resolve_launch_policy_fallback_uses_policy_list_order(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _write_agent_profile(
        tmp_path,
        name="reviewer",
        frontmatter=(
            "name: reviewer\n"
            "model: claude\n"
            "model-policies:\n"
            "  - match: {alias: opencode}\n"
            "    override: {effort: low}\n"
            "  - match: {alias: codex}\n"
            "    override: {effort: medium}\n"
        ),
    )
    claude = _mock_alias(alias="claude", model_id="claude-haiku-4-5", harness=HarnessId.CLAUDE)
    codex = _mock_alias(alias="codex", model_id="gpt-5.3-codex", harness=HarnessId.CODEX)
    opencode = _mock_alias(alias="opencode", model_id="kimi-k2.6", harness=HarnessId.OPENCODE)
    _patch_alias_resolution(
        monkeypatch,
        resolved_entries={
            "claude": claude,
            "claude-haiku-4-5": claude,
            "codex": codex,
            "gpt-5.3-codex": codex,
            "opencode": opencode,
            "kimi-k2.6": opencode,
        },
    )

    policy = resolve_launch_policy(
        SurfacePolicyInput(
            surface=LaunchCompositionSurface.SPAWN_PREPARE,
            catalog=CatalogSession(tmp_path),
            layers=(RuntimeOverrides(agent="reviewer"), RuntimeOverrides()),
            config_overrides=RuntimeOverrides.from_config(MeridianConfig()),
            config=MeridianConfig(),
            harness_registry=_registry_with_harnesses(HarnessId.CODEX, HarnessId.OPENCODE),
        )
    )

    assert policy.model == "kimi-k2.6"
    assert policy.harness == HarnessId.OPENCODE


def test_resolve_launch_policy_fallback_uses_combined_overlay_profile_list(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _write_agent_profile(
        tmp_path,
        name="reviewer",
        frontmatter=(
            "name: reviewer\n"
            "model: claude\n"
            "model-policies:\n"
            "  - match: {alias: codex}\n"
            "    override: {effort: medium}\n"
        ),
    )
    claude = _mock_alias(alias="claude", model_id="claude-haiku-4-5", harness=HarnessId.CLAUDE)
    codex = _mock_alias(alias="codex", model_id="gpt-5.3-codex", harness=HarnessId.CODEX)
    opencode = _mock_alias(alias="opencode", model_id="kimi-k2.6", harness=HarnessId.OPENCODE)
    _patch_alias_resolution(
        monkeypatch,
        resolved_entries={
            "claude": claude,
            "claude-haiku-4-5": claude,
            "codex": codex,
            "gpt-5.3-codex": codex,
            "opencode": opencode,
            "kimi-k2.6": opencode,
        },
    )

    config = MeridianConfig.model_validate(
        {
            "agents": {
                "reviewer": {
                    "model_policies": [
                        {
                            "match_type": "alias",
                            "match_value": "opencode",
                            "overrides": {"effort": "low"},
                        }
                    ]
                }
            }
        }
    )
    policy = resolve_launch_policy(
        SurfacePolicyInput(
            surface=LaunchCompositionSurface.SPAWN_PREPARE,
            catalog=CatalogSession(tmp_path),
            layers=(RuntimeOverrides(agent="reviewer"), RuntimeOverrides()),
            config_overrides=RuntimeOverrides.from_config(config),
            config=config,
            harness_registry=_registry_with_harnesses(HarnessId.CODEX, HarnessId.OPENCODE),
        )
    )

    assert policy.model == "kimi-k2.6"
    assert policy.harness == HarnessId.OPENCODE
    assert [candidate["token"] for candidate in policy.fallback_chain] == ["opencode", "codex"]


def test_resolve_launch_policy_overlay_no_fallback_rule_skips_overlay_candidate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _write_agent_profile(
        tmp_path,
        name="reviewer",
        frontmatter=(
            "name: reviewer\n"
            "model: claude\n"
            "model-policies:\n"
            "  - match: {alias: codex}\n"
            "    override: {effort: medium}\n"
        ),
    )
    claude = _mock_alias(alias="claude", model_id="claude-haiku-4-5", harness=HarnessId.CLAUDE)
    codex = _mock_alias(alias="codex", model_id="gpt-5.3-codex", harness=HarnessId.CODEX)
    opencode = _mock_alias(alias="opencode", model_id="kimi-k2.6", harness=HarnessId.OPENCODE)
    _patch_alias_resolution(
        monkeypatch,
        resolved_entries={
            "claude": claude,
            "claude-haiku-4-5": claude,
            "codex": codex,
            "gpt-5.3-codex": codex,
            "opencode": opencode,
            "kimi-k2.6": opencode,
        },
    )

    config = MeridianConfig.model_validate(
        {
            "agents": {
                "reviewer": {
                    "model_policies": [
                        {
                            "match_type": "alias",
                            "match_value": "opencode",
                            "overrides": {"effort": "low"},
                            "no_fallback": True,
                        }
                    ]
                }
            }
        }
    )
    policy = resolve_launch_policy(
        SurfacePolicyInput(
            surface=LaunchCompositionSurface.SPAWN_PREPARE,
            catalog=CatalogSession(tmp_path),
            layers=(RuntimeOverrides(agent="reviewer"), RuntimeOverrides()),
            config_overrides=RuntimeOverrides.from_config(config),
            config=config,
            harness_registry=_registry_with_harnesses(HarnessId.CODEX, HarnessId.OPENCODE),
        )
    )

    assert policy.model == "gpt-5.3-codex"
    assert policy.harness == HarnessId.CODEX
    assert [candidate["token"] for candidate in policy.fallback_chain] == ["codex"]


def test_resolve_launch_policy_explicit_model_suppresses_availability_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _write_agent_profile(
        tmp_path,
        name="reviewer",
        frontmatter=(
            "name: reviewer\n"
            "model-policies:\n"
            "  - match: {alias: codex}\n"
            "    override: {effort: medium}\n"
        ),
    )
    claude = _mock_alias(alias="claude", model_id="claude-haiku-4-5", harness=HarnessId.CLAUDE)
    codex = _mock_alias(alias="codex", model_id="gpt-5.3-codex", harness=HarnessId.CODEX)
    _patch_alias_resolution(
        monkeypatch,
        resolved_entries={
            "claude": claude,
            "claude-haiku-4-5": claude,
            "codex": codex,
            "gpt-5.3-codex": codex,
        },
    )

    with pytest.raises(ValueError, match="Unknown or unsupported harness 'claude'"):
        resolve_launch_policy(
            SurfacePolicyInput(
                surface=LaunchCompositionSurface.SPAWN_PREPARE,
                catalog=CatalogSession(tmp_path),
                layers=(
                    RuntimeOverrides(agent="reviewer", model="claude"),
                    RuntimeOverrides(),
                ),
                config_overrides=RuntimeOverrides.from_config(MeridianConfig()),
                config=MeridianConfig(),
                harness_registry=_registry_with_harnesses(HarnessId.CODEX),
            )
        )


def test_resolve_launch_policy_fallback_skips_unresolved_or_unavailable_candidates(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _write_agent_profile(
        tmp_path,
        name="reviewer",
        frontmatter=(
            "name: reviewer\n"
            "model: claude\n"
            "model-policies:\n"
            "  - match: {alias: missing}\n"
            "    override: {effort: low}\n"
            "  - match: {alias: claude}\n"
            "    override: {effort: medium}\n"
            "  - match: {alias: codex}\n"
            "    override: {effort: high}\n"
        ),
    )
    claude = _mock_alias(alias="claude", model_id="claude-haiku-4-5", harness=HarnessId.CLAUDE)
    codex = _mock_alias(alias="codex", model_id="gpt-5.3-codex", harness=HarnessId.CODEX)
    _patch_alias_resolution(
        monkeypatch,
        resolved_entries={
            "claude": claude,
            "claude-haiku-4-5": claude,
            "codex": codex,
            "gpt-5.3-codex": codex,
        },
    )

    policy = resolve_launch_policy(
        SurfacePolicyInput(
            surface=LaunchCompositionSurface.SPAWN_PREPARE,
            catalog=CatalogSession(tmp_path),
            layers=(RuntimeOverrides(agent="reviewer"), RuntimeOverrides()),
            config_overrides=RuntimeOverrides.from_config(MeridianConfig()),
            config=MeridianConfig(),
            harness_registry=_registry_with_harnesses(HarnessId.CODEX),
        )
    )

    assert policy.model == "gpt-5.3-codex"
    assert policy.harness == HarnessId.CODEX


def test_resolve_launch_policy_fallback_preserves_policy_harness_and_effort_overrides(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _write_agent_profile(
        tmp_path,
        name="reviewer",
        frontmatter=(
            "name: reviewer\n"
            "model: claude\n"
            "model-policies:\n"
            "  - match: {alias: gpt55}\n"
            "    override: {harness: opencode, effort: medium}\n"
        ),
    )
    claude = _mock_alias(alias="claude", model_id="claude-haiku-4-5", harness=HarnessId.CLAUDE)
    gpt55 = _mock_alias(
        alias="gpt55",
        model_id="gpt-5.5",
        harness=HarnessId.CODEX,
        default_effort="low",
    )
    _patch_alias_resolution(
        monkeypatch,
        resolved_entries={
            "claude": claude,
            "claude-haiku-4-5": claude,
            "gpt55": gpt55,
            "gpt-5.5": gpt55,
        },
    )

    policy = resolve_launch_policy(
        SurfacePolicyInput(
            surface=LaunchCompositionSurface.SPAWN_PREPARE,
            catalog=CatalogSession(tmp_path),
            layers=(RuntimeOverrides(agent="reviewer"), RuntimeOverrides()),
            config_overrides=RuntimeOverrides.from_config(MeridianConfig()),
            config=MeridianConfig(),
            harness_registry=_registry_with_harnesses(HarnessId.CODEX, HarnessId.OPENCODE),
        )
    )

    assert policy.model == "gpt-5.5"
    assert policy.harness == HarnessId.OPENCODE
    assert policy.execution_policy.effort == "medium"


def test_resolve_launch_policy_fallback_keeps_cli_effort_override(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _write_agent_profile(
        tmp_path,
        name="reviewer",
        frontmatter=(
            "name: reviewer\n"
            "model: claude\n"
            "model-policies:\n"
            "  - match: {alias: opencode}\n"
            "    override: {harness: opencode, effort: medium}\n"
        ),
    )
    claude = _mock_alias(alias="claude", model_id="claude-haiku-4-5", harness=HarnessId.CLAUDE)
    opencode = _mock_alias(alias="opencode", model_id="kimi-k2.6", harness=HarnessId.OPENCODE)
    _patch_alias_resolution(
        monkeypatch,
        resolved_entries={
            "claude": claude,
            "claude-haiku-4-5": claude,
            "opencode": opencode,
            "kimi-k2.6": opencode,
        },
    )

    policy = resolve_launch_policy(
        SurfacePolicyInput(
            surface=LaunchCompositionSurface.SPAWN_PREPARE,
            catalog=CatalogSession(tmp_path),
            layers=(
                RuntimeOverrides(agent="reviewer", effort="high"),
                RuntimeOverrides(),
            ),
            config_overrides=RuntimeOverrides.from_config(MeridianConfig()),
            config=MeridianConfig(),
            harness_registry=_registry_with_harnesses(HarnessId.OPENCODE),
        )
    )

    assert policy.model == "kimi-k2.6"
    assert policy.harness == HarnessId.OPENCODE
    assert policy.execution_policy.effort == "high"


def test_resolve_launch_policy_fallback_does_not_recursively_reapply_model_policies(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _write_agent_profile(
        tmp_path,
        name="reviewer",
        frontmatter=(
            "name: reviewer\n"
            "model: claude\n"
            "model-policies:\n"
            "  - match: {alias: opencode}\n"
            "    override: {harness: opencode, effort: medium}\n"
            "  - match: {model: kimi-k2.6}\n"
            "    override: {harness: codex, effort: low}\n"
        ),
    )
    claude = _mock_alias(alias="claude", model_id="claude-haiku-4-5", harness=HarnessId.CLAUDE)
    opencode = _mock_alias(alias="opencode", model_id="kimi-k2.6", harness=HarnessId.OPENCODE)
    _patch_alias_resolution(
        monkeypatch,
        resolved_entries={
            "claude": claude,
            "claude-haiku-4-5": claude,
            "opencode": opencode,
            "kimi-k2.6": opencode,
        },
    )

    policy = resolve_launch_policy(
        SurfacePolicyInput(
            surface=LaunchCompositionSurface.SPAWN_PREPARE,
            catalog=CatalogSession(tmp_path),
            layers=(RuntimeOverrides(agent="reviewer"), RuntimeOverrides()),
            config_overrides=RuntimeOverrides.from_config(MeridianConfig()),
            config=MeridianConfig(),
            harness_registry=_registry_with_harnesses(HarnessId.CODEX, HarnessId.OPENCODE),
        )
    )

    assert policy.model == "kimi-k2.6"
    assert policy.harness == HarnessId.OPENCODE
    assert policy.execution_policy.effort == "medium"


def test_resolve_launch_policy_demoted_candidate_remains_policy_stripped(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _write_agent_profile(
        tmp_path,
        name="reviewer",
        frontmatter=(
            "name: reviewer\n"
            "model: gpt55\n"
            "model-policies:\n"
            "  - match: {alias: gpt55}\n"
            "    override: {harness: claude, effort: medium}\n"
        ),
    )
    gpt55 = _mock_alias(
        alias="gpt55",
        model_id="gpt-5.5",
        harness=HarnessId.CODEX,
        default_effort="low",
    )
    _patch_alias_resolution(
        monkeypatch,
        resolved_entries={
            "gpt55": gpt55,
            "gpt-5.5": gpt55,
        },
    )

    policy = resolve_launch_policy(
        SurfacePolicyInput(
            surface=LaunchCompositionSurface.SPAWN_PREPARE,
            catalog=CatalogSession(tmp_path),
            layers=(RuntimeOverrides(agent="reviewer"), RuntimeOverrides()),
            config_overrides=RuntimeOverrides.from_config(MeridianConfig()),
            config=MeridianConfig(),
            harness_registry=_registry_with_harnesses(HarnessId.CODEX),
        )
    )

    assert policy.model == "gpt-5.5"
    assert policy.harness == HarnessId.CODEX
    assert policy.execution_policy.effort == "low"
