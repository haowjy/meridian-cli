"""Agent profile parser for `.mars/agents/*.md`."""
from collections.abc import Mapping
import logging
from contextlib import suppress
from pathlib import Path
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from meridian.lib.catalog.skill import files_have_equal_text, split_markdown_frontmatter
from meridian.lib.config.project_root import resolve_project_root_resolution
from meridian.lib.core.overrides import (
    KNOWN_APPROVAL_VALUES,
    KNOWN_EFFORT_VALUES,
    AutocompactPctValue,
    AutocompactValue,
    validate_autocompact_pct_value,
    validate_autocompact_value,
)
from meridian.lib.tools import ToolsField, parse_tools_field

# Re-export under private names for backward compatibility within this module.
_KNOWN_EFFORT_VALUES = KNOWN_EFFORT_VALUES
_KNOWN_APPROVAL_VALUES = KNOWN_APPROVAL_VALUES
_MODEL_POLICY_SCALAR_OVERRIDE_KEYS = frozenset(
    {"harness", "sandbox", "approval", "effort", "autocompact", "autocompact_pct", "timeout"}
)
_MODEL_POLICY_DEFERRED_LIST_OVERRIDE_KEYS = frozenset(
    {"skills", "tools", "mcp-tools"}
)
_MODEL_POLICY_OVERRIDE_KEYS = (
    _MODEL_POLICY_SCALAR_OVERRIDE_KEYS | _MODEL_POLICY_DEFERRED_LIST_OVERRIDE_KEYS
)


logger = logging.getLogger(__name__)


class AgentModelEntry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)

    effort: str | None = None
    autocompact: AutocompactValue = None
    autocompact_pct: AutocompactPctValue = None

    @field_validator("effort")
    @classmethod
    def _validate_effort(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if normalized not in _KNOWN_EFFORT_VALUES:
            raise ValueError(f"expected one of {sorted(_KNOWN_EFFORT_VALUES)}")
        return normalized


class ModelPolicyRule(BaseModel):
    """One model-policy override rule from profile frontmatter."""

    model_config = ConfigDict(frozen=True)

    match_type: Literal["model", "alias", "model-glob"]
    match_value: str
    no_fallback: bool = False
    overrides: Mapping[str, object]


class AgentProfile(BaseModel):
    """Parsed agent profile with frontmatter defaults + markdown body."""

    model_config = ConfigDict(frozen=True)

    name: str
    description: str
    model: str | None
    harness: str | None = None
    skills: tuple[str, ...]
    tools: ToolsField | None
    mcp_tools: tuple[str, ...]
    sandbox: str | None
    effort: str | None
    approval: str | None = None
    autocompact: int | None = None
    autocompact_pct: int | None = None
    mode: Literal["primary", "subagent"] = "subagent"
    model_policies: tuple[ModelPolicyRule, ...] = ()
    models: Mapping[str, AgentModelEntry] = Field(default_factory=dict)
    model_invocable: bool = True
    body: str
    path: Path
    raw_content: str


def _normalize_string_list(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        normalized = value.strip()
        return (normalized,) if normalized else ()
    if isinstance(value, list):
        values = [str(item).strip() for item in cast("list[object]", value) if str(item).strip()]
        return tuple(values)
    return ()


def _normalize_deduplicated(value: object) -> tuple[str, ...]:
    """Normalize a string list and deduplicate while preserving order."""
    parsed = _normalize_string_list(value)
    seen: set[str] = set()
    result: list[str] = []
    for item in parsed:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return tuple(result)


def _parse_model_overrides(
    raw_models: object,
    *,
    profile_name: str,
) -> dict[str, AgentModelEntry]:
    _ = profile_name
    if raw_models is None:
        return {}
    if not isinstance(raw_models, Mapping):
        return {}

    parsed: dict[str, AgentModelEntry] = {}
    for raw_key, raw_value in cast("Mapping[object, object]", raw_models).items():
        key = str(raw_key).strip()
        if not key:
            continue
        if not isinstance(raw_value, Mapping):
            continue
        try:
            parsed[key] = AgentModelEntry.model_validate(raw_value)
        except ValidationError:
            continue
    return parsed


def _parse_model_policies(
    raw_policies: object,
    *,
    profile_name: str,
) -> tuple[ModelPolicyRule, ...]:
    if raw_policies is None:
        return ()
    if not isinstance(raw_policies, list):
        raise ValueError(
            f"Agent profile '{profile_name}' has invalid model-policies: expected list."
        )

    parsed: list[ModelPolicyRule] = []
    allowed_match_keys = {"model", "alias", "model-glob"}
    legacy_fallback_key = "fallback" + "-order"
    for index, raw_rule in enumerate(cast("list[object]", raw_policies), start=1):
        if not isinstance(raw_rule, Mapping):
            raise ValueError(
                f"Agent profile '{profile_name}' has invalid model-policies[{index}]: "
                "expected mapping."
            )
        rule = cast("Mapping[object, object]", raw_rule)
        raw_match = rule.get("match")
        raw_override = rule.get("override")
        if not isinstance(raw_match, Mapping):
            raise ValueError(
                f"Agent profile '{profile_name}' has invalid model-policies[{index}].match: "
                "expected mapping."
            )
        match = cast("Mapping[object, object]", raw_match)
        normalized_match = {str(key).strip(): value for key, value in match.items()}
        if len(normalized_match) != 1:
            raise ValueError(
                f"Agent profile '{profile_name}' model-policies[{index}] must have exactly "
                "one match key: model, alias, or model-glob."
            )
        match_key = next(iter(normalized_match))
        if match_key not in allowed_match_keys:
            raise ValueError(
                f"Agent profile '{profile_name}' model-policies[{index}] has unknown match "
                f"key '{match_key}': expected model, alias, or model-glob."
            )
        match_value = str(normalized_match.get(match_key, "")).strip()
        if not match_value:
            raise ValueError(
                f"Agent profile '{profile_name}' model-policies[{index}] match '{match_key}' "
                "must not be empty."
            )
        if not isinstance(raw_override, Mapping):
            raise ValueError(
                f"Agent profile '{profile_name}' has invalid model-policies[{index}].override: "
                "expected mapping."
            )
        if any(str(key).strip() == legacy_fallback_key for key in rule):
            raise ValueError(
                f"Agent profile '{profile_name}' model-policies[{index}] uses "
                "'fallback-order' which is no longer supported. Remove fallback-order — "
                "fallback order is now implicit from list position. Use 'no-fallback: true' "
                "to exclude a rule from fallback."
            )
        raw_no_fallback = rule.get("no-fallback", False)
        if raw_no_fallback is None:
            no_fallback = False
        elif isinstance(raw_no_fallback, bool):
            no_fallback = raw_no_fallback
        else:
            raise ValueError(
                f"Agent profile '{profile_name}' model-policies[{index}] "
                "no-fallback must be true or false."
            )
        if match_key == "model-glob":
            no_fallback = True
        overrides = {
            str(key).strip(): value
            for key, value in cast("Mapping[object, object]", raw_override).items()
            if str(key).strip()
        }
        is_fallback_candidate = match_key in {"alias", "model"} and not no_fallback
        if not overrides and not is_fallback_candidate:
            raise ValueError(
                f"Agent profile '{profile_name}' model-policies[{index}] must set at least "
                "one override field."
            )
        unknown_keys = sorted(set(overrides) - _MODEL_POLICY_OVERRIDE_KEYS)
        if unknown_keys:
            allowed = ", ".join(sorted(_MODEL_POLICY_OVERRIDE_KEYS))
            raise ValueError(
                f"Agent profile '{profile_name}' model-policies[{index}] has unknown "
                f"override key '{unknown_keys[0]}': expected one of {allowed}."
            )
        parsed.append(
            ModelPolicyRule(
                match_type=cast("Literal['model', 'alias', 'model-glob']", match_key),
                match_value=match_value,
                no_fallback=no_fallback,
                overrides=overrides,
            )
        )
    return tuple(parsed)


def parse_agent_profile(path: Path) -> AgentProfile:
    """Parse a single markdown agent profile file."""

    markdown = path.read_text(encoding="utf-8")
    frontmatter, body = split_markdown_frontmatter(markdown)

    name_value = frontmatter.get("name")
    description_value = frontmatter.get("description")
    model_value = frontmatter.get("model")
    harness_value = frontmatter.get("harness")
    sandbox_value = frontmatter.get("sandbox")
    effort_value = frontmatter.get("effort")
    approval_value = frontmatter.get("approval")
    autocompact_value = frontmatter.get("autocompact")
    autocompact_pct_value = frontmatter.get("autocompact_pct")
    mode_value = frontmatter.get("mode")
    model_policies_value = frontmatter.get("model-policies")
    models_value = frontmatter.get("models")
    model_invocable_value = frontmatter.get("model-invocable")

    profile_name = str(name_value).strip() if name_value is not None else path.stem
    if frontmatter.get("fanout") is not None:
        raise ValueError(
            f"Agent profile '{profile_name}' contains 'fanout' which is no longer supported. "
            "Declare fallback candidates in model-policies list order instead. "
            "Use 'no-fallback: true' to exclude a rule."
        )
    model_invocable = (
        model_invocable_value if isinstance(model_invocable_value, bool) else True
    )
    sandbox = str(sandbox_value).strip() if sandbox_value is not None else None
    effort = str(effort_value).strip() if effort_value is not None else None

    approval = str(approval_value).strip() if approval_value is not None else None
    if approval is not None and approval and approval not in _KNOWN_APPROVAL_VALUES:
        approval = None

    autocompact: int | None = None
    if autocompact_value is not None:
        try:
            raw_autocompact = int(str(autocompact_value))
        except (TypeError, ValueError):
            pass
        else:
            with suppress(ValueError):
                autocompact = cast("int | None", validate_autocompact_value(raw_autocompact))

    autocompact_pct: int | None = None
    if autocompact_pct_value is not None:
        try:
            raw_autocompact_pct = int(str(autocompact_pct_value))
        except (TypeError, ValueError):
            pass
        else:
            with suppress(ValueError):
                autocompact_pct = cast(
                    "int | None", validate_autocompact_pct_value(raw_autocompact_pct)
                )

    models = _parse_model_overrides(models_value, profile_name=profile_name)
    model_policies = _parse_model_policies(
        model_policies_value,
        profile_name=profile_name,
    )

    mode = str(mode_value).strip() if mode_value is not None else "subagent"
    if mode not in {"primary", "subagent"}:
        raise ValueError(
            f"Agent profile '{profile_name}' has invalid mode '{mode}': "
            "expected 'primary' or 'subagent'."
        )

    if models_value is not None and model_policies_value is None:
        logger.warning(
            "Agent profile '%s' uses legacy models without model-policies; "
            "models is deprecated for policy overrides.",
            profile_name,
        )

    return AgentProfile(
        name=profile_name,
        description=str(description_value).strip() if description_value is not None else "",
        model=str(model_value).strip() if model_value is not None else None,
        harness=str(harness_value).strip() if harness_value is not None else None,
        skills=_normalize_string_list(frontmatter.get("skills")),
        tools=parse_tools_field(frontmatter.get("tools"), source=f"{profile_name}.tools"),
        mcp_tools=_normalize_deduplicated(frontmatter.get("mcp-tools")),
        sandbox=sandbox,
        effort=effort,
        approval=approval,
        autocompact=autocompact,
        autocompact_pct=autocompact_pct,
        mode=cast("Literal['primary', 'subagent']", mode),
        model_policies=model_policies,
        models=models,
        model_invocable=model_invocable,
        body=body,
        path=path.resolve(),
        raw_content=markdown,
    )


def _agent_search_dirs(project_root: Path) -> list[Path]:
    return [project_root / ".mars" / "agents"]


def scan_agent_profiles(
    project_root: Path | None = None,
    search_dirs: list[Path] | None = None,
    *,
    search_paths: object | None = None,
) -> list[AgentProfile]:
    """Parse all agent profiles from configured search directories."""

    root = resolve_project_root_resolution(project_root).project_root
    _ = search_paths
    directories = search_dirs if search_dirs is not None else _agent_search_dirs(root)
    profiles: list[AgentProfile] = []
    selected_by_name: dict[str, AgentProfile] = {}

    for directory in directories:
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.md")):
            profile = parse_agent_profile(path)
            existing = selected_by_name.get(profile.name)
            if existing is not None:
                if files_have_equal_text(existing.path, profile.path):
                    continue
                continue
            selected_by_name[profile.name] = profile
            profiles.append(profile)
    return profiles


def load_agent_profile(
    name: str,
    project_root: Path | None = None,
    *,
    search_paths: object | None = None,
) -> AgentProfile:
    """Load one agent profile by filename stem or frontmatter name."""

    normalized = name.strip()
    if not normalized:
        raise ValueError("Agent profile name must not be empty.")

    root = resolve_project_root_resolution(project_root).project_root

    for profile in scan_agent_profiles(project_root=root, search_paths=search_paths):
        if profile.path.stem == normalized or profile.name == normalized:
            return profile

    expected_path = Path(".mars") / "agents" / f"{normalized}.md"
    raise FileNotFoundError(
        "\n".join(
            (
                f"Agent '{normalized}' not found.",
                "",
                f"Expected: {expected_path.as_posix()}",
                "",
                "Run `meridian mars sync` to populate your agents directory, "
                "or see README.md for manual setup.",
            )
        )
    )
