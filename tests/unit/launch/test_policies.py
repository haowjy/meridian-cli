from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from meridian.lib.catalog.catalog_session import CatalogSession
from meridian.lib.catalog.model_aliases import AliasEntry
from meridian.lib.config.settings import MeridianConfig, load_config
from meridian.lib.core.overrides import RuntimeOverrides
from meridian.lib.core.types import HarnessId, ModelId
from meridian.lib.harness.registry import get_default_harness_registry
from meridian.lib.launch import bundle_adapter
from meridian.lib.launch.compiler import ModelPolicyRule, ProvenanceLevel
from meridian.lib.launch.launch_types import ResolvedExecutionPolicy
from meridian.lib.launch.policies import (
    SurfacePolicyInput,
    match_model_policy,
    resolve_launch_policy,
    resolve_policy_fields,
)
from meridian.lib.launch.request import LaunchCompositionSurface


@dataclass(frozen=True)
class _FakeBundleResult:
    model: str
    model_token: str
    harness: HarnessId
    harness_model: str | None
    execution_policy: ResolvedExecutionPolicy
    provenance: dict[str, str]
    warnings: tuple[str, ...] = ()
    tools_allowed: tuple[str, ...] = ()
    tools_disallowed: tuple[str, ...] = ()
    tools_mcp: tuple[str, ...] = ()


def _write_agent_profile(project_root: Path, *, name: str, frontmatter: str) -> None:
    path = project_root / ".mars" / "agents" / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{frontmatter}\n---\n\n# {name}\n", encoding="utf-8")


def _mock_alias(
    *,
    alias: str,
    model_id: str,
    harness: HarnessId,
) -> AliasEntry:
    return AliasEntry(
        alias=alias,
        model_id=ModelId(model_id),
        resolved_harness=harness,
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


def test_match_model_policy_first_match_wins_by_list_order(tmp_path: Path) -> None:
    winner = match_model_policy(
        model_policies=(
            ModelPolicyRule(
                match_type="model-glob",
                match_value="gpt-*",
                overrides={"effort": "low"},
            ),
            ModelPolicyRule(
                match_type="alias",
                match_value="fast",
                overrides={"effort": "medium"},
            ),
            ModelPolicyRule(
                match_type="model",
                match_value="gpt-5.5",
                overrides={"effort": "high"},
            ),
        ),
        canonical_model_id="gpt-5.5",
        selected_model_token="fast",
    )

    assert winner is not None
    assert winner.match_type == "model-glob"
    assert winner.match_value == "gpt-*"


def test_spawn_prepare_ignores_removed_defaults_routing_keys(
    monkeypatch,
    tmp_path: Path,
) -> None:
    (tmp_path / "meridian.toml").write_text(
        '[defaults]\nmodel = "gpt55"\nharness = "opencode"\n',
        encoding="utf-8",
    )
    config = load_config(tmp_path, resolve_models=False)
    captured: dict[str, object] = {}

    def fake_request(
        request: bundle_adapter.BundleRequest,
        *,
        harness_registry: object,
    ) -> _FakeBundleResult:
        _ = harness_registry
        captured["request"] = request
        return _FakeBundleResult(
            model="gpt-5.5",
            model_token="gpt55",
            harness=HarnessId.CODEX,
            harness_model="gpt-5.5",
            execution_policy=ResolvedExecutionPolicy(),
            provenance={"model_source": "project", "harness_source": "project"},
        )

    monkeypatch.setattr(bundle_adapter, "request_and_resolve", fake_request)
    monkeypatch.setattr(CatalogSession, "alias_map", lambda self: {})

    resolve_launch_policy(
        SurfacePolicyInput(
            surface=LaunchCompositionSurface.SPAWN_PREPARE,
            catalog=CatalogSession(tmp_path),
            layers=(RuntimeOverrides(), RuntimeOverrides()),
            config_overrides=RuntimeOverrides.from_spawn_config(config),
            config=config,
            harness_registry=get_default_harness_registry(),
        )
    )

    request = captured["request"]
    assert isinstance(request, bundle_adapter.BundleRequest)
    assert request.model_override is None
    assert request.harness_override is None


def test_primary_config_defaults_do_not_flow_into_bundle_routing_overrides(
    monkeypatch,
    tmp_path: Path,
) -> None:
    config = MeridianConfig.model_validate(
        {
            "primary": {
                "model": "haiku",
                "harness": "opencode",
                "effort": "medium",
                "timeout": 30.0,
            }
        }
    )
    captured: dict[str, object] = {}

    def fake_request(
        request: bundle_adapter.BundleRequest,
        *,
        harness_registry: object,
    ) -> _FakeBundleResult:
        _ = harness_registry
        captured["request"] = request
        return _FakeBundleResult(
            model="claude-haiku-4-5",
            model_token="haiku",
            harness=HarnessId.OPENCODE,
            harness_model="openrouter/anthropic/claude-haiku-4.5",
            execution_policy=ResolvedExecutionPolicy(),
            provenance={},
        )

    monkeypatch.setattr(bundle_adapter, "request_and_resolve", fake_request)
    monkeypatch.setattr(CatalogSession, "alias_map", lambda self: {})

    policy = resolve_launch_policy(
        SurfacePolicyInput(
            surface=LaunchCompositionSurface.PRIMARY,
            catalog=CatalogSession(tmp_path),
            layers=(RuntimeOverrides(), RuntimeOverrides()),
            config_overrides=RuntimeOverrides.from_config(config),
            config=config,
            harness_registry=get_default_harness_registry(),
        )
    )

    request = captured["request"]
    assert isinstance(request, bundle_adapter.BundleRequest)
    assert request.model_override is None
    assert request.harness_override is None
    assert policy.model == "claude-haiku-4-5"
    assert policy.harness == HarnessId.OPENCODE
    assert policy.model_selection is not None
    assert policy.model_selection.harness_model_id == "openrouter/anthropic/claude-haiku-4.5"
    assert policy.execution_policy.effort == "medium"
    assert policy.execution_policy.timeout == 30.0
    assert policy.field_provenance.model_source is ProvenanceLevel.UNSET
    assert policy.field_provenance.harness_source is ProvenanceLevel.UNSET
    assert policy.field_provenance.effort_source is ProvenanceLevel.CONFIG_DEFAULT
    assert policy.field_provenance.timeout_source is ProvenanceLevel.CONFIG_DEFAULT


def test_primary_cli_model_demotes_lower_config_harness(
    monkeypatch,
    tmp_path: Path,
) -> None:
    config = MeridianConfig.model_validate({"primary": {"harness": "opencode"}})
    captured: dict[str, object] = {}

    def fake_request(
        request: bundle_adapter.BundleRequest,
        *,
        harness_registry: object,
    ) -> _FakeBundleResult:
        _ = harness_registry
        captured["request"] = request
        return _FakeBundleResult(
            model="gpt-5.5",
            model_token="gpt55",
            harness=HarnessId.CODEX,
            harness_model="gpt-5.5",
            execution_policy=ResolvedExecutionPolicy(),
            provenance={"model_source": "cli", "harness_source": "provider"},
        )

    monkeypatch.setattr(bundle_adapter, "request_and_resolve", fake_request)
    monkeypatch.setattr(CatalogSession, "alias_map", lambda self: {})

    policy = resolve_launch_policy(
        SurfacePolicyInput(
            surface=LaunchCompositionSurface.PRIMARY,
            catalog=CatalogSession(tmp_path),
            layers=(RuntimeOverrides(model="gpt55"), RuntimeOverrides()),
            config_overrides=RuntimeOverrides.from_config(config),
            config=config,
            harness_registry=get_default_harness_registry(),
        )
    )

    request = captured["request"]
    assert isinstance(request, bundle_adapter.BundleRequest)
    assert request.model_override == "gpt55"
    assert request.harness_override is None
    assert policy.harness == HarnessId.CODEX
    assert policy.field_provenance.model_source is ProvenanceLevel.CLI


def test_spawn_prepare_profile_routing_does_not_flow_into_bundle_request(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _write_agent_profile(
        tmp_path,
        name="reviewer",
        frontmatter="name: reviewer\nmodel: stale-route\nharness: claude\n",
    )

    captured: dict[str, object] = {}

    def fake_request(
        request: bundle_adapter.BundleRequest,
        *,
        harness_registry: object,
    ) -> _FakeBundleResult:
        _ = harness_registry
        captured["request"] = request
        return _FakeBundleResult(
            model="gpt-5.5",
            model_token="gpt55",
            harness=HarnessId.CODEX,
            harness_model="gpt-5.5",
            execution_policy=ResolvedExecutionPolicy(),
            provenance={},
        )

    monkeypatch.setattr(bundle_adapter, "request_and_resolve", fake_request)
    monkeypatch.setattr(CatalogSession, "alias_map", lambda self: {})

    resolve_launch_policy(
        SurfacePolicyInput(
            surface=LaunchCompositionSurface.SPAWN_PREPARE,
            catalog=CatalogSession(tmp_path),
            layers=(RuntimeOverrides(agent="reviewer"), RuntimeOverrides()),
            config_overrides=RuntimeOverrides.from_spawn_config(MeridianConfig()),
            config=MeridianConfig(),
            harness_registry=get_default_harness_registry(),
        )
    )

    request = captured["request"]
    assert isinstance(request, bundle_adapter.BundleRequest)
    assert request.model_override is None
    assert request.harness_override is None


def test_spawn_prepare_env_timeout_provenance(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        bundle_adapter,
        "request_and_resolve",
        lambda request, *, harness_registry: _FakeBundleResult(
            model="gpt-5.5",
            model_token="gpt55",
            harness=HarnessId.CODEX,
            harness_model="gpt-5.5",
            execution_policy=ResolvedExecutionPolicy(),
            provenance={},
        ),
    )
    monkeypatch.setattr(CatalogSession, "alias_map", lambda self: {})

    config = MeridianConfig()
    policy = resolve_launch_policy(
        SurfacePolicyInput(
            surface=LaunchCompositionSurface.SPAWN_PREPARE,
            catalog=CatalogSession(tmp_path),
            layers=(RuntimeOverrides(), RuntimeOverrides(timeout=25.0)),
            config_overrides=RuntimeOverrides.from_spawn_config(config),
            config=config,
            harness_registry=get_default_harness_registry(),
        )
    )

    assert policy.execution_policy.timeout == 25.0
    assert policy.field_provenance.timeout_source is ProvenanceLevel.ENV


def test_spawn_prepare_cli_timeout_provenance(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        bundle_adapter,
        "request_and_resolve",
        lambda request, *, harness_registry: _FakeBundleResult(
            model="gpt-5.5",
            model_token="gpt55",
            harness=HarnessId.CODEX,
            harness_model="gpt-5.5",
            execution_policy=ResolvedExecutionPolicy(timeout=45.0),
            provenance={"timeout_source": "profile-default"},
        ),
    )
    monkeypatch.setattr(CatalogSession, "alias_map", lambda self: {})

    config = MeridianConfig()
    policy = resolve_launch_policy(
        SurfacePolicyInput(
            surface=LaunchCompositionSurface.SPAWN_PREPARE,
            catalog=CatalogSession(tmp_path),
            layers=(RuntimeOverrides(timeout=12.0), RuntimeOverrides()),
            config_overrides=RuntimeOverrides.from_spawn_config(config),
            config=config,
            harness_registry=get_default_harness_registry(),
        )
    )

    assert policy.execution_policy.timeout == 12.0
    assert policy.field_provenance.timeout_source is ProvenanceLevel.CLI

def test_spawn_prepare_bundle_config_provenance_maps_to_config_default(
    monkeypatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}

    def fake_request(
        request: bundle_adapter.BundleRequest,
        *,
        harness_registry: object,
    ) -> _FakeBundleResult:
        _ = harness_registry
        captured["request"] = request
        return _FakeBundleResult(
            model="gpt-5.5",
            model_token="gpt55",
            harness=HarnessId.CODEX,
            harness_model="gpt-5.5",
            execution_policy=ResolvedExecutionPolicy(),
            provenance={"model_source": "project", "harness_source": "config"},
        )

    monkeypatch.setattr(bundle_adapter, "request_and_resolve", fake_request)
    monkeypatch.setattr(CatalogSession, "alias_map", lambda self: {})

    policy = resolve_launch_policy(
        SurfacePolicyInput(
            surface=LaunchCompositionSurface.SPAWN_PREPARE,
            catalog=CatalogSession(tmp_path),
            layers=(RuntimeOverrides(), RuntimeOverrides()),
            config_overrides=RuntimeOverrides.from_spawn_config(MeridianConfig()),
            config=MeridianConfig(),
            harness_registry=get_default_harness_registry(),
        )
    )

    request = captured["request"]
    assert isinstance(request, bundle_adapter.BundleRequest)
    assert request.model_override is None
    assert request.harness_override is None
    assert policy.field_provenance.model_source is ProvenanceLevel.CONFIG_DEFAULT
    assert policy.field_provenance.harness_source is ProvenanceLevel.CONFIG_DEFAULT


def test_spawn_prepare_bundle_project_policy_provenance_maps_to_config_default(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        bundle_adapter,
        "request_and_resolve",
        lambda request, *, harness_registry: _FakeBundleResult(
            model="gpt-5.5",
            model_token="gpt55",
            harness=HarnessId.CODEX,
            harness_model="gpt-5.5",
            execution_policy=ResolvedExecutionPolicy(effort="medium"),
            provenance={"effort_source": "project"},
        ),
    )
    monkeypatch.setattr(CatalogSession, "alias_map", lambda self: {})

    policy = resolve_launch_policy(
        SurfacePolicyInput(
            surface=LaunchCompositionSurface.SPAWN_PREPARE,
            catalog=CatalogSession(tmp_path),
            layers=(RuntimeOverrides(), RuntimeOverrides()),
            config_overrides=RuntimeOverrides.from_spawn_config(MeridianConfig()),
            config=MeridianConfig(),
            harness_registry=get_default_harness_registry(),
        )
    )

    assert policy.execution_policy.effort == "medium"
    assert policy.field_provenance.effort_source is ProvenanceLevel.CONFIG_DEFAULT

def test_spawn_prepare_warns_missing_runnable_path_for_bundle_reroute(
    monkeypatch,
    tmp_path: Path,
) -> None:
    alias = _mock_alias(alias="fast-gpt55", model_id="gpt-5.5", harness=HarnessId.CODEX)

    monkeypatch.setattr(
        CatalogSession,
        "resolve_model",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("spawn-prepare reroute warning should use alias_map, not resolve_model")
        ),
    )
    monkeypatch.setattr(
        bundle_adapter,
        "request_and_resolve",
        lambda request, *, harness_registry: _FakeBundleResult(
            model="gpt-5.5",
            model_token="gpt55",
            harness=HarnessId.OPENCODE,
            harness_model=None,
            execution_policy=ResolvedExecutionPolicy(),
            provenance={"harness_source": "project"},
        ),
    )
    monkeypatch.setattr(
        CatalogSession,
        "alias_map",
        lambda self: {"fast-gpt55": alias},
    )

    policy = resolve_launch_policy(
        SurfacePolicyInput(
            surface=LaunchCompositionSurface.SPAWN_PREPARE,
            catalog=CatalogSession(tmp_path),
            layers=(RuntimeOverrides(model="gpt55"), RuntimeOverrides()),
            config_overrides=RuntimeOverrides.from_spawn_config(MeridianConfig()),
            config=MeridianConfig(),
            harness_registry=get_default_harness_registry(),
        )
    )

    assert any(warning.code == "missing_runnable_path" for warning in policy.warnings)


def test_spawn_prepare_passes_profile_and_requested_skills_to_bundle(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _write_agent_profile(
        tmp_path,
        name="reviewer",
        frontmatter="name: reviewer\nmodel: gpt55\nskills: [alpha, beta]\n",
    )

    captured: dict[str, object] = {}

    def fake_request(
        request: bundle_adapter.BundleRequest,
        *,
        harness_registry: object,
    ) -> _FakeBundleResult:
        _ = harness_registry
        captured["skills"] = request.extra_skills
        return _FakeBundleResult(
            model="gpt-5.5",
            model_token="gpt55",
            harness=HarnessId.CODEX,
            harness_model="gpt-5.5",
            execution_policy=ResolvedExecutionPolicy(),
            provenance={"harness_source": "provider"},
        )

    monkeypatch.setattr(bundle_adapter, "request_and_resolve", fake_request)
    monkeypatch.setattr(CatalogSession, "alias_map", lambda self: {})

    resolve_launch_policy(
        SurfacePolicyInput(
            surface=LaunchCompositionSurface.SPAWN_PREPARE,
            catalog=CatalogSession(tmp_path),
            layers=(RuntimeOverrides(agent="reviewer"), RuntimeOverrides()),
            config_overrides=RuntimeOverrides.from_spawn_config(MeridianConfig()),
            config=MeridianConfig(),
            harness_registry=get_default_harness_registry(),
            requested_skills=("beta", "gamma"),
        )
    )

    assert captured["skills"] == ("alpha", "beta", "gamma")
