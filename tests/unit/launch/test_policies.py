from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from meridian.lib.catalog.agent import load_agent_profile
from meridian.lib.catalog.catalog_session import CatalogSession
from meridian.lib.catalog.model_aliases import AliasEntry
from meridian.lib.config.settings import MeridianConfig
from meridian.lib.core.overrides import RuntimeOverrides
from meridian.lib.core.types import HarnessId, ModelId
from meridian.lib.harness.registry import get_default_harness_registry
from meridian.lib.launch import bundle_adapter
from meridian.lib.launch.compiler import ProvenanceLevel
from meridian.lib.launch.launch_types import ResolvedExecutionPolicy
from meridian.lib.launch.policies import (
    ModelSelectionContext,
    SurfacePolicyInput,
    match_model_policy,
    resolve_launch_policy,
    resolve_policy_fields,
    validate_harness_compatibility,
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
    prompt_surface_system_instruction: str = ""
    prompt_surface_supplemental_documents: tuple[object, ...] = ()
    prompt_surface_inventory_prompt: str = ""
    tools_allowed: tuple[str, ...] = ()
    tools_disallowed: tuple[str, ...] = ()
    tools_mcp: tuple[str, ...] = ()
    skills_loaded: tuple[str, ...] = ()
    skills_missing: tuple[str, ...] = ()


def _write_agent_profile(project_root: Path, *, name: str, frontmatter: str) -> None:
    path = project_root / ".mars" / "agents" / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{frontmatter}\n---\n\n# {name}\n", encoding="utf-8")


def _mock_alias(
    *,
    alias: str,
    model_id: str,
    harness: HarnessId = HarnessId.CODEX,
) -> AliasEntry:
    return AliasEntry(
        alias=alias,
        model_id=ModelId(model_id),
        resolved_harness=harness,
    )


def _patch_local_alias_resolution(
    monkeypatch,
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

    winner = match_model_policy(
        model_policies=profile.model_policies,
        canonical_model_id="gpt-5.5",
        selected_model_token="fast",
    )

    assert winner is not None
    assert winner.match_type == "model-glob"


def test_validate_harness_compatibility_allows_cross_candidate_route() -> None:
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


def test_spawn_prepare_uses_bundle_adapter_not_catalog_resolve_model(
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
            harness=HarnessId.OPENCODE,
            harness_model="openai/gpt-5.5",
            execution_policy=ResolvedExecutionPolicy(effort="medium"),
            provenance={"model_source": "cli", "harness_source": "cli", "effort_source": "cli"},
        )

    monkeypatch.setattr(bundle_adapter, "request_and_resolve", fake_request)
    monkeypatch.setattr(
        CatalogSession,
        "resolve_model",
        lambda *_args, **_kwargs: (
            _ for _ in ()
        ).throw(AssertionError("spawn routing must not call CatalogSession.resolve_model")),
    )
    monkeypatch.setattr(CatalogSession, "alias_map", lambda self: {})

    policy = resolve_launch_policy(
        SurfacePolicyInput(
            surface=LaunchCompositionSurface.SPAWN_PREPARE,
            catalog=CatalogSession(tmp_path),
            layers=(RuntimeOverrides(model="gpt55", harness="opencode"), RuntimeOverrides()),
            config_overrides=RuntimeOverrides.from_config(MeridianConfig()),
            config=MeridianConfig(),
            harness_registry=get_default_harness_registry(),
        )
    )

    assert policy.model == "gpt-5.5"
    assert policy.harness == HarnessId.OPENCODE
    assert policy.model_selection is not None
    assert policy.model_selection.harness_model_id == "openai/gpt-5.5"

    request = captured["request"]
    assert isinstance(request, bundle_adapter.BundleRequest)
    assert request.model_override == "gpt55"
    assert request.harness_override == "opencode"


def test_spawn_prepare_overlay_policy_applies_after_bundle(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _write_agent_profile(
        tmp_path,
        name="reviewer",
        frontmatter="name: reviewer\nmodel: gpt55\n",
    )

    monkeypatch.setattr(
        bundle_adapter,
        "request_and_resolve",
        lambda request, *, harness_registry: _FakeBundleResult(
            model="gpt-5.5",
            model_token="gpt55",
            harness=HarnessId.CODEX,
            harness_model="gpt-5.5",
            execution_policy=ResolvedExecutionPolicy(effort="low", sandbox="read-only"),
            provenance={"model_source": "profile-default", "harness_source": "provider"},
        ),
    )
    monkeypatch.setattr(CatalogSession, "alias_map", lambda self: {})

    config = MeridianConfig.model_validate(
        {
            "agents": {
                "reviewer": {
                    "effort": "high",
                    "sandbox": "workspace-write",
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
            harness_registry=get_default_harness_registry(),
        )
    )

    assert policy.execution_policy.effort == "high"
    assert policy.execution_policy.sandbox == "workspace-write"


def test_spawn_prepare_cli_policy_beats_agent_overlay(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _write_agent_profile(
        tmp_path,
        name="reviewer",
        frontmatter="name: reviewer\nmodel: gpt55\n",
    )

    monkeypatch.setattr(
        bundle_adapter,
        "request_and_resolve",
        lambda request, *, harness_registry: _FakeBundleResult(
            model="gpt-5.5",
            model_token="gpt55",
            harness=HarnessId.CODEX,
            harness_model="gpt-5.5",
            execution_policy=ResolvedExecutionPolicy(effort="medium"),
            provenance={"effort_source": "cli", "harness_source": "provider"},
        ),
    )
    monkeypatch.setattr(CatalogSession, "alias_map", lambda self: {})

    config = MeridianConfig.model_validate({"agents": {"reviewer": {"effort": "low"}}})
    policy = resolve_launch_policy(
        SurfacePolicyInput(
            surface=LaunchCompositionSurface.SPAWN_PREPARE,
            catalog=CatalogSession(tmp_path),
            layers=(RuntimeOverrides(agent="reviewer", effort="high"), RuntimeOverrides()),
            config_overrides=RuntimeOverrides.from_config(config),
            config=config,
            harness_registry=get_default_harness_registry(),
        )
    )

    assert policy.execution_policy.effort == "high"


def test_spawn_prepare_agent_overlay_routing_overrides_bundle_when_cli_absent(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _write_agent_profile(
        tmp_path,
        name="reviewer",
        frontmatter="name: reviewer\nmodel: gpt55\n",
    )

    captured_requests: list[bundle_adapter.BundleRequest] = []

    def fake_request(
        request: bundle_adapter.BundleRequest,
        *,
        harness_registry: object,
    ) -> _FakeBundleResult:
        _ = harness_registry
        captured_requests.append(request)
        if len(captured_requests) == 1:
            assert request.model_override is None
            assert request.harness_override is None
            return _FakeBundleResult(
                model="gpt-5.5",
                model_token="gpt55",
                harness=HarnessId.CODEX,
                harness_model="openai/gpt-5.5",
                execution_policy=ResolvedExecutionPolicy(),
                provenance={"model_source": "profile-default", "harness_source": "provider"},
            )
        assert request.model_override == "haiku"
        assert request.harness_override == "opencode"
        return _FakeBundleResult(
            model="claude-haiku-4-5",
            model_token="haiku",
            harness=HarnessId.OPENCODE,
            harness_model="openrouter/anthropic/claude-haiku-4.5",
            execution_policy=ResolvedExecutionPolicy(),
            provenance={"model_source": "cli", "harness_source": "cli"},
        )

    monkeypatch.setattr(bundle_adapter, "request_and_resolve", fake_request)
    monkeypatch.setattr(CatalogSession, "alias_map", lambda self: {})

    config = MeridianConfig.model_validate(
        {"agents": {"reviewer": {"model": "haiku", "harness": "opencode"}}}
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

    assert len(captured_requests) == 2
    assert policy.model == "claude-haiku-4-5"
    assert policy.harness == HarnessId.OPENCODE
    assert policy.model_selection is not None
    assert policy.model_selection.harness_model_id == "openrouter/anthropic/claude-haiku-4.5"
    assert policy.model_selection.harness_provenance == "agent-overlay-default"
    assert policy.field_provenance.model_source is ProvenanceLevel.AGENT_OVERLAY_DEFAULT
    assert policy.field_provenance.harness_source is ProvenanceLevel.AGENT_OVERLAY_DEFAULT


def test_spawn_prepare_cli_routing_beats_agent_overlay(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _write_agent_profile(
        tmp_path,
        name="reviewer",
        frontmatter="name: reviewer\nmodel: gpt55\n",
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
            harness=HarnessId.OPENCODE,
            harness_model="openai/gpt-5.5",
            execution_policy=ResolvedExecutionPolicy(),
            provenance={"model_source": "cli", "harness_source": "cli"},
        )

    monkeypatch.setattr(bundle_adapter, "request_and_resolve", fake_request)
    monkeypatch.setattr(CatalogSession, "alias_map", lambda self: {})

    config = MeridianConfig.model_validate(
        {"agents": {"reviewer": {"model": "haiku", "harness": "claude"}}}
    )
    policy = resolve_launch_policy(
        SurfacePolicyInput(
            surface=LaunchCompositionSurface.SPAWN_PREPARE,
            catalog=CatalogSession(tmp_path),
            layers=(
                RuntimeOverrides(agent="reviewer", model="gpt55", harness="opencode"),
                RuntimeOverrides(),
            ),
            config_overrides=RuntimeOverrides.from_config(config),
            config=config,
            harness_registry=get_default_harness_registry(),
        )
    )

    request = captured["request"]
    assert isinstance(request, bundle_adapter.BundleRequest)
    assert request.model_override == "gpt55"
    assert request.harness_override == "opencode"
    assert policy.field_provenance.model_source is ProvenanceLevel.CLI
    assert policy.field_provenance.harness_source is ProvenanceLevel.CLI


def test_chat_surface_keeps_local_compiler_path(
    monkeypatch,
) -> None:
    alias = _mock_alias(alias="codex", model_id="gpt-5.3-codex", harness=HarnessId.CODEX)
    _patch_local_alias_resolution(
        monkeypatch,
        resolved_entries={"codex": alias, "gpt-5.3-codex": alias},
    )
    monkeypatch.setattr(
        bundle_adapter,
        "request_and_resolve",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("chat should not call bundle adapter in phase 1")
        ),
    )

    policy = resolve_launch_policy(
        SurfacePolicyInput(
            surface=LaunchCompositionSurface.CHAT,
            catalog=CatalogSession(Path.cwd()),
            layers=(RuntimeOverrides(model="codex"), RuntimeOverrides()),
            config_overrides=RuntimeOverrides.from_config(MeridianConfig()),
            config=MeridianConfig(),
            harness_registry=get_default_harness_registry(),
        )
    )

    assert policy.model == "gpt-5.3-codex"
    assert policy.harness == HarnessId.CODEX


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
            config_overrides=RuntimeOverrides.from_config(MeridianConfig()),
            config=MeridianConfig(),
            harness_registry=get_default_harness_registry(),
            requested_skills=("beta", "gamma"),
        )
    )

    assert captured["skills"] == ("alpha", "beta", "gamma")
