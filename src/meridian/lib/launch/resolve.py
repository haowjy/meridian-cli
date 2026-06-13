"""Shared launch-time resolution helpers for launch orchestration."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from pydantic import BaseModel, ConfigDict

from meridian.lib.catalog.agent import AgentProfile, load_agent_profile
from meridian.lib.catalog.model_aliases import AliasEntry
from meridian.lib.catalog.skill import SkillRegistry
from meridian.lib.config.settings import MeridianConfig
from meridian.lib.core.domain import SkillContent
from meridian.lib.core.types import HarnessId, ModelId
from meridian.lib.harness.registry import HarnessRegistry
from meridian.lib.utils.time import minutes_to_seconds


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


def dedupe_skill_contents(skills: Sequence[SkillContent]) -> tuple[SkillContent, ...]:
    """De-duplicate loaded skill payloads by skill name preserving order."""

    seen: set[str] = set()
    ordered: list[SkillContent] = []
    for skill in skills:
        if skill.name in seen:
            continue
        seen.add(skill.name)
        ordered.append(skill)
    return tuple(ordered)


def load_skill_contents(
    registry: SkillRegistry,
    names: Sequence[str],
    *,
    harness_id: str | None = None,
    selected_model_token: str | None = None,
    canonical_model_id: str | None = None,
) -> tuple[SkillContent, ...]:
    """Load skill contents in deterministic deduplicated order."""

    deduped_names = dedupe_skill_names(names)
    if not deduped_names:
        return ()
    loaded = registry.load(
        list(deduped_names),
        harness_id=harness_id,
        selected_model_token=selected_model_token,
        canonical_model_id=canonical_model_id,
    )
    return dedupe_skill_contents(loaded)


@dataclass(frozen=True)
class AgentLaunchInput:
    """Resolved agent selection from a raw CLI ``--agent`` value."""

    agent: str | None = None
    agent_opt_out: bool = False


def resolve_agent_launch_input(raw_agent: str | None) -> AgentLaunchInput:
    """Map raw ``--agent`` input to launch request fields.

    Three states:
    - omitted (``None``) -> unset; fall through to ``primary.agent`` / inheritance
    - ``-a ""`` -> explicit no agent
    - ``-a coder`` -> named profile
    """

    if raw_agent is None:
        return AgentLaunchInput()
    stripped = raw_agent.strip()
    if not stripped:
        return AgentLaunchInput(agent_opt_out=True)
    return AgentLaunchInput(agent=stripped)


def load_agent_profile_with_fallback(
    *,
    project_root: Path,
    requested_agent: str | None = None,
    configured_default: str | None = None,
    agent_opt_out: bool = False,
) -> tuple[AgentProfile | None, str | None]:
    """Load agent profile with a standard fallback chain.

    Resolution order:
    1. requested_agent (explicit --agent flag) -> load or raise
    2. configured_default (from config) -> try load
    3. None (no profile)

    When ``agent_opt_out`` is true (``-a ""``), skip all profiles including the
    configured default.
    """

    if agent_opt_out:
        return None, None

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
) -> None:
    """Validate that the selected harness is available for primary launch."""

    _ = model, model_entry

    supported_primary_harnesses = tuple(
        harness_id_candidate
        for harness_id_candidate in harness_registry.ids()
        if harness_registry.get(harness_id_candidate).capabilities.supports_primary_launch
    )
    supported_primary_set = set(supported_primary_harnesses)
    if harness_id not in supported_primary_set:
        supported_text = ", ".join(str(harness) for harness in supported_primary_harnesses)
        raise ValueError(f"Unsupported harness '{harness_id}'. Expected one of: {supported_text}.")


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
    )
    return override_harness


def _coerce_positive_timeout_seconds(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        timeout = float(value)
        return timeout if timeout > 0 else None
    if isinstance(value, str):
        normalized = value.strip()
        if not normalized:
            return None
        try:
            timeout = float(normalized)
        except ValueError:
            return None
        return timeout if timeout > 0 else None
    return None


def _resolve_pi_child_wave_timeout_from_config_snapshot(
    config_snapshot: dict[str, object],
) -> float | None:
    try:
        config = MeridianConfig.model_validate(config_snapshot)
    except Exception:
        config = None
    if config is not None and config.pi_child_wave_timeout_seconds is not None:
        timeout_seconds = _coerce_positive_timeout_seconds(
            config.pi_child_wave_timeout_seconds
        )
        if timeout_seconds is not None:
            return timeout_seconds

    raw_seconds = config_snapshot.get("pi_child_wave_timeout_seconds")
    timeout_seconds = _coerce_positive_timeout_seconds(raw_seconds)
    if timeout_seconds is not None:
        return timeout_seconds

    timeouts_section = config_snapshot.get("timeouts")
    if isinstance(timeouts_section, dict):
        typed_timeouts_section = cast("dict[str, object]", timeouts_section)
        section_value = typed_timeouts_section.get("pi_child_wave_timeout_seconds")
        timeout_seconds = _coerce_positive_timeout_seconds(section_value)
        if timeout_seconds is not None:
            return timeout_seconds
    return None


def _resolve_timeout_field_from_config_snapshot(
    config_snapshot: dict[str, object],
    field_name: str,
) -> float | None:
    try:
        config = MeridianConfig.model_validate(config_snapshot)
    except Exception:
        config = None
    if config is not None:
        timeout_seconds = _coerce_positive_timeout_seconds(getattr(config, field_name, None))
        if timeout_seconds is not None:
            return timeout_seconds

    timeout_seconds = _coerce_positive_timeout_seconds(config_snapshot.get(field_name))
    if timeout_seconds is not None:
        return timeout_seconds

    timeouts_section = config_snapshot.get("timeouts")
    if isinstance(timeouts_section, dict):
        typed_timeouts_section = cast("dict[str, object]", timeouts_section)
        timeout_seconds = _coerce_positive_timeout_seconds(typed_timeouts_section.get(field_name))
        if timeout_seconds is not None:
            return timeout_seconds
    return None


def resolve_pi_notification_timeout_seconds(
    *,
    explicit_timeout_seconds: float | None,
    config_snapshot: dict[str, object] | None,
) -> float | None:
    """Resolve Pi continuation-notification timeout from spawn wait semantics.

    Preference order:
    1) Explicit spawn execution timeout (when provided)
    2) Configured wait timeout minutes from the launch config snapshot
    """

    if explicit_timeout_seconds is not None and explicit_timeout_seconds > 0:
        return float(explicit_timeout_seconds)
    if not config_snapshot:
        return None
    try:
        config = MeridianConfig.model_validate(config_snapshot)
    except Exception:
        return None
    wait_timeout_seconds = minutes_to_seconds(config.wait_timeout_minutes)
    if wait_timeout_seconds is None or wait_timeout_seconds <= 0:
        return None
    return float(wait_timeout_seconds)


def parse_duration_seconds(value: str | None) -> float | None:
    """Parse a duration token such as ``90m``, ``1h``, or ``3300`` (seconds)."""

    if value is None:
        return None
    normalized = value.strip().lower()
    if not normalized:
        return None
    multipliers = (
        ("ms", 0.001),
        ("s", 1.0),
        ("m", 60.0),
        ("h", 3600.0),
    )
    for suffix, multiplier in multipliers:
        if normalized.endswith(suffix):
            number_text = normalized[: -len(suffix)].strip()
            if not number_text:
                return None
            try:
                parsed = float(number_text)
            except ValueError:
                return None
            return parsed * multiplier if parsed > 0 else None
    try:
        parsed = float(normalized)
    except ValueError:
        return None
    return parsed if parsed > 0 else None


def _resolve_pi_task_ping_interval_from_config_snapshot(
    config_snapshot: dict[str, object],
) -> float | None:
    try:
        config = MeridianConfig.model_validate(config_snapshot)
    except Exception:
        config = None
    if config is not None and config.pi_task_ping_interval_seconds is not None:
        interval_seconds = _coerce_positive_timeout_seconds(
            config.pi_task_ping_interval_seconds
        )
        if interval_seconds is not None:
            return interval_seconds

    raw_seconds = config_snapshot.get("pi_task_ping_interval_seconds")
    interval_seconds = _coerce_positive_timeout_seconds(raw_seconds)
    if interval_seconds is not None:
        return interval_seconds

    timeouts_section = config_snapshot.get("timeouts")
    if isinstance(timeouts_section, dict):
        typed_timeouts_section = cast("dict[str, object]", timeouts_section)
        section_value = typed_timeouts_section.get("pi_task_ping_interval_seconds")
        interval_seconds = _coerce_positive_timeout_seconds(section_value)
        if interval_seconds is not None:
            return interval_seconds
    return None


def resolve_pi_task_ping_interval_seconds(
    *,
    explicit_interval_seconds: float | None,
    config_snapshot: dict[str, object] | None,
) -> float | None:
    """Resolve Pi background-task ping interval in seconds (None = extension default)."""

    if explicit_interval_seconds is not None and explicit_interval_seconds > 0:
        return float(explicit_interval_seconds)
    if config_snapshot:
        timeout_from_config = _resolve_pi_task_ping_interval_from_config_snapshot(config_snapshot)
        if timeout_from_config is not None:
            return timeout_from_config
    return None


def resolve_pi_child_wave_timeout_seconds(
    *,
    explicit_timeout_seconds: float | None,
    config_snapshot: dict[str, object] | None,
) -> float:
    """Resolve Pi tracked-child wave timeout in seconds.

    Preference order:
    1) Explicit per-spawn override (when provided)
    2) Configured timeout from launch config snapshot
    3) Default 300s
    """

    if explicit_timeout_seconds is not None and explicit_timeout_seconds > 0:
        return float(explicit_timeout_seconds)
    if config_snapshot:
        timeout_from_config = _resolve_pi_child_wave_timeout_from_config_snapshot(config_snapshot)
        if timeout_from_config is not None:
            return timeout_from_config
    return 300.0


def resolve_resident_deadline_seconds(
    *,
    config_snapshot: dict[str, object] | None,
) -> float:
    """Resolve Codex/OpenCode resident hard-cap deadline in seconds."""

    if config_snapshot:
        timeout_from_config = _resolve_timeout_field_from_config_snapshot(
            config_snapshot,
            "resident_deadline_seconds",
        )
        if timeout_from_config is not None:
            return timeout_from_config
    return 1800.0


def resolve_resident_poll_seconds(
    *,
    config_snapshot: dict[str, object] | None,
) -> float:
    """Resolve Codex/OpenCode resident outstanding-work poll interval."""

    if config_snapshot:
        timeout_from_config = _resolve_timeout_field_from_config_snapshot(
            config_snapshot,
            "resident_poll_seconds",
        )
        if timeout_from_config is not None:
            return timeout_from_config
    return 10.0


__all__ = [
    "AgentLaunchInput",
    "ResolvedSkills",
    "dedupe_skill_names",
    "format_missing_skills_warning",
    "load_agent_profile_with_fallback",
    "parse_duration_seconds",
    "resolve_agent_launch_input",
    "resolve_harness",
    "resolve_pi_child_wave_timeout_seconds",
    "resolve_pi_notification_timeout_seconds",
    "resolve_pi_task_ping_interval_seconds",
    "resolve_profile_path",
    "resolve_resident_deadline_seconds",
    "resolve_resident_poll_seconds",
    "resolve_skill_paths",
    "resolve_skills_from_profile",
    "validate_harness_compatibility",
]
