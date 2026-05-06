from pathlib import Path

import pytest

from meridian.lib.catalog.catalog_session import CatalogSession
from meridian.lib.catalog.model_aliases import AliasEntry
from meridian.lib.config.settings import MeridianConfig
from meridian.lib.core.overrides import RuntimeOverrides
from meridian.lib.core.types import HarnessId, ModelId
from meridian.lib.harness.codex import CodexAdapter
from meridian.lib.harness.registry import HarnessRegistry
from meridian.lib.launch.policies import resolve_policies as _resolve_policies_impl


def _write_minimal_mars_config(project_root: Path) -> None:
    (project_root / "mars.toml").write_text(
        "[settings]\n"
        'targets = [".claude"]\n',
        encoding="utf-8",
    )


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


def _patch_alias_resolution(
    monkeypatch: pytest.MonkeyPatch,
    *,
    resolved_entries: dict[str, AliasEntry],
    catalog_entries: list[AliasEntry] | None = None,
) -> None:
    def resolve_entry(self: CatalogSession, name: str) -> AliasEntry:
        _ = self
        return resolved_entries.get(name, _mock_alias(alias="", model_id=name))

    def list_entries(self: CatalogSession) -> list[AliasEntry]:
        _ = self
        return catalog_entries if catalog_entries is not None else list(resolved_entries.values())

    monkeypatch.setattr(CatalogSession, "resolve_model", resolve_entry)
    monkeypatch.setattr(CatalogSession, "load_aliases", list_entries)


def resolve_policies(*, project_root: Path, **kwargs):
    return _resolve_policies_impl(
        catalog=CatalogSession(project_root),
        **kwargs,
    )


def test_overlay_model_policies_replace_profile_candidates_for_availability_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_minimal_mars_config(tmp_path)
    _write_agent_profile(
        tmp_path,
        name="reviewer",
        frontmatter=(
            "name: reviewer\n"
            "model: claude-choice\n"
            "model-policies:\n"
            "  - match: {alias: profile-unavailable}\n"
            "    override: {effort: low}\n"
        ),
    )
    aliases = {
        "claude-choice": _mock_alias(
            alias="claude-choice",
            model_id="claude-haiku-4-5",
            harness=HarnessId.CLAUDE,
        ),
        "profile-unavailable": _mock_alias(
            alias="profile-unavailable",
            model_id="opencode-profile",
            harness=HarnessId.OPENCODE,
        ),
        "overlay-codex": _mock_alias(
            alias="overlay-codex",
            model_id="gpt-5.5",
            harness=HarnessId.CODEX,
        ),
    }
    _patch_alias_resolution(monkeypatch, resolved_entries=aliases)

    registry = HarnessRegistry()
    registry.register(CodexAdapter())
    config = MeridianConfig.model_validate(
        {
            "agents": {
                "reviewer": {
                    "model_policies": [
                        {
                            "match_type": "alias",
                            "match_value": "overlay-codex",
                            "overrides": {"effort": "high"},
                        }
                    ]
                }
            }
        }
    )

    policies = resolve_policies(
        project_root=tmp_path,
        layers=(RuntimeOverrides(agent="reviewer"), RuntimeOverrides()),
        config_overrides=RuntimeOverrides(),
        config=config,
        harness_registry=registry,
        configured_default_harness="claude",
    )

    assert policies.model == "gpt-5.5"
    assert policies.harness == HarnessId.CODEX
    assert policies.model_selection is not None
    assert policies.model_selection.requested_token == "claude-choice"
    assert policies.model_selection.selected_model_token == "overlay-codex"
    assert policies.model_selection.canonical_model_id == "gpt-5.5"
    assert policies.model_selection.harness_provenance == "availability-fallback"


def test_overlay_empty_model_policies_suppress_profile_fallback_candidates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_minimal_mars_config(tmp_path)
    _write_agent_profile(
        tmp_path,
        name="reviewer",
        frontmatter=(
            "name: reviewer\n"
            "model: claude-choice\n"
            "model-policies:\n"
            "  - match: {alias: profile-codex}\n"
            "    override: {effort: high}\n"
        ),
    )
    aliases = {
        "claude-choice": _mock_alias(
            alias="claude-choice",
            model_id="claude-haiku-4-5",
            harness=HarnessId.CLAUDE,
        ),
        "profile-codex": _mock_alias(
            alias="profile-codex",
            model_id="gpt-5.5",
            harness=HarnessId.CODEX,
        ),
    }
    _patch_alias_resolution(monkeypatch, resolved_entries=aliases)

    registry = HarnessRegistry()
    registry.register(CodexAdapter())
    config = MeridianConfig.model_validate(
        {
            "agents": {
                "reviewer": {
                    "model_policies": []
                }
            }
        }
    )

    with pytest.raises(ValueError, match="Unknown or unsupported harness 'claude'"):
        resolve_policies(
            project_root=tmp_path,
            layers=(RuntimeOverrides(agent="reviewer"), RuntimeOverrides()),
            config_overrides=RuntimeOverrides(),
            config=config,
            harness_registry=registry,
            configured_default_harness="claude",
        )
