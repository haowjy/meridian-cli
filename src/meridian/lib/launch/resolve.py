"""Shared launch-time resolution helpers for launch orchestration."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from meridian.lib.catalog.agent import AgentProfile, load_agent_profile
from meridian.lib.catalog.model_aliases import AliasEntry
from meridian.lib.catalog.skill import SkillRegistry
from meridian.lib.core.domain import SkillContent
from meridian.lib.core.types import HarnessId, ModelId
from meridian.lib.harness.registry import HarnessRegistry


def dedupe_skill_names(names: Iterable[str]) -> tuple[str, ...]:
    """Normalize and de-duplicate skill names while preserving first-seen order."""

    seen: set[str] = set()
    ordered: list[str] = []
    for raw in names:
        normalized = raw.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(normalized)
    return tuple(ordered)


def load_agent_profile_with_fallback(
    *,
    project_root: Path,
    requested_agent: str | None = None,
    configured_default: str | None = None,
) -> tuple[AgentProfile | None, str | None]:
    """Load agent profile with a standard fallback chain.

    Resolution order:
    1. requested_agent (explicit --agent flag) -> load or raise
    2. configured_default (from config) -> try load
    3. None (no profile)
    """

    requested_profile = requested_agent.strip() if requested_agent is not None else ""
    if requested_profile:
        return (
            load_agent_profile(
                requested_profile,
                project_root=project_root,
            ),
            None,
        )

    configured_profile = configured_default.strip() if configured_default is not None else ""
    if configured_profile:
        try:
            return (
                load_agent_profile(
                    configured_profile,
                    project_root=project_root,
                ),
                None,
            )
        except FileNotFoundError:
            return (
                None,
                "Configured agent profile "
                f"'{configured_profile}' is unavailable; running without an agent profile.",
            )

    return None, None


class ResolvedSkills(BaseModel):
    model_config = ConfigDict(frozen=True)

    skill_names: tuple[str, ...]
    loaded_skills: tuple[SkillContent, ...]
    missing_skills: tuple[str, ...]


def resolve_skills_from_profile(
    *,
    profile_skills: tuple[str, ...],
    project_root: Path,
    readonly: bool = False,
    harness_id: str | None = None,
    selected_model_token: str | None = None,
    canonical_model_id: str | None = None,
) -> ResolvedSkills:
    """Load and resolve skills declared in an agent profile."""

    registry = SkillRegistry(
        project_root=project_root,
        readonly=readonly,
    )
    manifests = registry.list_skills()
    if not manifests and not registry.readonly:
        registry.reindex()
        manifests = registry.list_skills()

    available_skill_names = {item.name for item in manifests}
    missing_skills = tuple(
        skill_name for skill_name in profile_skills if skill_name not in available_skill_names
    )
    resolved_skill_names = tuple(
        skill_name for skill_name in profile_skills if skill_name in available_skill_names
    )
    from .prompt import load_skill_contents

    loaded_skills = load_skill_contents(
        registry,
        resolved_skill_names,
        harness_id=harness_id,
        selected_model_token=selected_model_token,
        canonical_model_id=canonical_model_id,
    )
    return ResolvedSkills(
        skill_names=tuple(skill.name for skill in loaded_skills),
        loaded_skills=loaded_skills,
        missing_skills=missing_skills,
    )


def format_missing_skills_warning(missing_skills: tuple[str, ...]) -> str:
    """Render a user-facing warning for unavailable skills."""

    if not missing_skills:
        return ""

    expected_paths = [f".mars/skills/{skill}/SKILL.md" for skill in missing_skills]
    expected_lines = [f"Expected: {expected_paths[0]}"]
    expected_lines.extend(f"         {path}" for path in expected_paths[1:])
    return "\n".join(
        (
            f"Warning: Skipped unavailable skills: {', '.join(missing_skills)}",
            *expected_lines,
            "Run `meridian mars sync` to install missing skills.",
        )
    )


def resolve_profile_path(profile: AgentProfile | None) -> str:
    if profile is None:
        return ""
    if profile.path.is_absolute() and profile.path.exists():
        return profile.path.resolve().as_posix()
    return ""


def resolve_skill_paths(loaded_skills: tuple[SkillContent, ...]) -> tuple[str, ...]:
    return tuple(Path(skill.path).expanduser().resolve().as_posix() for skill in loaded_skills)


def validate_harness_compatibility(
    *,
    model: str,
    harness_id: HarnessId,
    model_entry: AliasEntry | None,
    harness_registry: HarnessRegistry,
    harness_source: str = "resolved",
) -> None:
    """Validate harness/model compatibility with candidate-aware routing."""

    supported_primary_harnesses = tuple(
        harness_id_candidate
        for harness_id_candidate in harness_registry.ids()
        if harness_registry.get(harness_id_candidate).capabilities.supports_primary_launch
    )
    supported_primary_set = set(supported_primary_harnesses)
    if harness_id not in supported_primary_set:
        supported_text = ", ".join(str(harness) for harness in supported_primary_harnesses)
        raise ValueError(f"Unsupported harness '{harness_id}'. Expected one of: {supported_text}.")

    if model_entry is None:
        return

    harness_candidates = tuple(model_entry.harness_candidates or ())
    if harness_candidates:
        if str(harness_id) not in harness_candidates:
            supported = ", ".join(harness_candidates)
            source_note = " from model-policy override" if harness_source == "model-policy" else ""
            raise ValueError(
                f"Harness '{harness_id}'{source_note} is not a supported route "
                f"for model '{model}'. Supported harnesses: {supported}."
            )
        return

    if harness_id != model_entry.harness:
        source_note = " from model-policy override" if harness_source == "model-policy" else ""
        raise ValueError(
            f"Harness '{harness_id}'{source_note} is incompatible with model '{model}' "
            f"(routes to '{model_entry.harness}')."
        )


def resolve_harness(
    *,
    model: ModelId,
    model_entry: AliasEntry | None,
    harness_override: str | None,
    harness_registry: HarnessRegistry,
) -> HarnessId:
    """Determine final primary-launch harness from a resolved model entry."""

    normalized_override = (harness_override or "").strip()
    if not normalized_override:
        if model_entry is None:
            raise ValueError(
                f"Unknown model '{model}'. Run `meridian mars models list` "
                "to inspect supported models."
            )
        return model_entry.harness

    override_harness = HarnessId(normalized_override)
    validate_harness_compatibility(
        model=str(model),
        harness_id=override_harness,
        model_entry=model_entry,
        harness_registry=harness_registry,
        harness_source="resolved",
    )
    return override_harness


def select_harness_model_id(
    *,
    model_entry: AliasEntry | None,
    harness_id: HarnessId,
    canonical_model_id: str,
) -> str:
    """Select the harness-specific model ID for the chosen route.

    Returns canonical_model_id when no harness-specific ID is available.
    """
    if model_entry is None:
        return canonical_model_id

    specific_id = model_entry.harness_model_id_for(str(harness_id))
    if isinstance(specific_id, str):
        return specific_id
    return canonical_model_id


__all__ = [
    "ResolvedSkills",
    "dedupe_skill_names",
    "format_missing_skills_warning",
    "load_agent_profile_with_fallback",
    "resolve_harness",
    "resolve_profile_path",
    "resolve_skill_paths",
    "resolve_skills_from_profile",
    "select_harness_model_id",
    "validate_harness_compatibility",
]
