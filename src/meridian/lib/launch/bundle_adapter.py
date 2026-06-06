"""Mars launch-bundle adapter for spawn/chat launch policy routing."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import TYPE_CHECKING, cast

from meridian.lib.core.types import HarnessId
from meridian.lib.launch.compiler import FieldProvenance, ProvenanceLevel
from meridian.lib.launch.composition import AvailableSkillEntry
from meridian.lib.launch.launch_types import (
    CompositionWarning,
    ResolvedExecutionPolicy,
    TerminalSurfaceMode,
)
from meridian.lib.tools import ToolAction, ToolsField

if TYPE_CHECKING:
    from meridian.lib.catalog.agent import AgentProfile
    from meridian.lib.catalog.model_aliases import AliasEntry
    from meridian.lib.harness.registry import HarnessRegistry
    from meridian.lib.launch.policies import ModelSelectionContext, ResolvedLaunchPolicy
    from meridian.lib.launch.resolve import ResolvedSkills

def _resolve_mars_min_version() -> str:
    try:
        return version("mars-agents")
    except PackageNotFoundError:
        return "unknown"


_MARS_BUNDLE_MIN_VERSION = _resolve_mars_min_version()
_SUPPORTED_BUNDLE_SCHEMA_VERSIONS = (3,)


@dataclass(frozen=True)
class BundleRequest:
    """Input to launch-bundle resolution."""

    agent: str | None
    project_root: Path
    model_override: str | None = None
    harness_override: str | None = None
    effort_override: str | None = None
    approval_override: str | None = None
    sandbox_override: str | None = None
    extra_skills: tuple[str, ...] = ()


@dataclass(frozen=True)
class LoadedSkillEntry:
    """Structured loaded skill from the bundle (name + type + body)."""

    name: str
    skill_type: str
    body: str


@dataclass(frozen=True)
class _BundleResult:
    model: str
    model_token: str
    harness: HarnessId
    harness_model: str | None
    execution_policy: ResolvedExecutionPolicy
    provenance: dict[str, str]
    warnings: tuple[str, ...]
    prompt_surface_inventory_prompt: str
    tools_allowed: tuple[str, ...]
    tools_disallowed: tuple[str, ...]
    tools_mcp: tuple[str, ...]
    skills_loaded: tuple[LoadedSkillEntry, ...]
    skills_available: tuple[AvailableSkillEntry, ...]
    skills_missing: tuple[str, ...]


def _resolve_mars_binary() -> str | None:
    scripts_dir = Path(sys.executable).parent
    for name in ("mars", "mars.exe"):
        candidate = scripts_dir / name
        if candidate.is_file():
            return str(candidate)
    return shutil.which("mars")


def _normalize_str(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()


def _as_str_dict(raw: object) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    values: dict[str, str] = {}
    for key, value in cast("dict[object, object]", raw).items():
        key_str = str(key).strip()
        value_str = _normalize_str(value)
        if key_str and value_str:
            values[key_str] = value_str
    return values


def _as_str_tuple(raw: object) -> tuple[str, ...]:
    if not isinstance(raw, list):
        return ()
    values: list[str] = []
    for item in cast("list[object]", raw):
        normalized = _normalize_str(item)
        if normalized:
            values.append(normalized)
    return tuple(values)


def _parse_skills_loaded(raw: object) -> tuple[LoadedSkillEntry, ...]:
    """Parse skills.loaded — list of {name, skill_type, body} objects."""
    if not isinstance(raw, list):
        return ()
    entries: list[LoadedSkillEntry] = []
    for item in cast("list[object]", raw):
        if isinstance(item, dict):
            item_dict = cast("dict[str, object]", item)
            name = _normalize_str(item_dict.get("name"))
            if name:
                skill_type = _normalize_str(item_dict.get("skill_type")) or "reference"
                body = _normalize_str(item_dict.get("body"))
                entries.append(LoadedSkillEntry(name=name, skill_type=skill_type, body=body))
    return tuple(entries)


def _parse_skills_available(raw: object) -> tuple[AvailableSkillEntry, ...]:
    """Parse v3 skills.available field — list of {name, skill_type, description} objects."""
    if not isinstance(raw, list):
        return ()
    entries: list[AvailableSkillEntry] = []
    for item in cast("list[object]", raw):
        if isinstance(item, dict):
            item_dict = cast("dict[str, object]", item)
            name = _normalize_str(item_dict.get("name"))
            skill_type = _normalize_str(item_dict.get("skill_type")) or "reference"
            if name:
                entries.append(AvailableSkillEntry(name=name, skill_type=skill_type))
    return tuple(entries)


def _bundle_schema_error(message: str) -> RuntimeError:
    return RuntimeError(
        f"Mars launch-bundle returned invalid schema: {message}. "
        f"Meridian requires mars >= {_MARS_BUNDLE_MIN_VERSION}."
    )


def _required_object_field(payload: dict[str, object], field_name: str) -> dict[str, object]:
    raw_value = payload.get(field_name)
    if not isinstance(raw_value, dict):
        raise _bundle_schema_error(f"'{field_name}' must be an object")
    return cast("dict[str, object]", raw_value)


def _optional_object_field(payload: dict[str, object], field_name: str) -> dict[str, object]:
    if field_name not in payload or payload[field_name] is None:
        return {}
    raw_value = payload[field_name]
    if not isinstance(raw_value, dict):
        raise _bundle_schema_error(f"'{field_name}' must be an object when present")
    return cast("dict[str, object]", raw_value)


def _parse_harness_id(
    raw_harness: object,
    *,
    harness_registry: HarnessRegistry,
) -> HarnessId:
    harness_token = _normalize_str(raw_harness)
    if not harness_token:
        raise _bundle_schema_error("routing.harness is empty")
    try:
        harness_id = HarnessId(harness_token)
    except ValueError as exc:
        supported = ", ".join(str(harness) for harness in harness_registry.ids())
        raise ValueError(
            "Mars launch-bundle returned unsupported routing.harness "
            f"'{harness_token}'. Available harnesses: {supported}. "
            f"Meridian requires mars >= {_MARS_BUNDLE_MIN_VERSION}."
        ) from exc
    try:
        harness_registry.get_subprocess_harness(harness_id)
    except (KeyError, TypeError) as exc:
        supported = ", ".join(str(harness) for harness in harness_registry.ids())
        raise ValueError(
            "Mars launch-bundle returned unavailable routing.harness "
            f"'{harness_token}'. Available harnesses: {supported}. "
            f"Meridian requires mars >= {_MARS_BUNDLE_MIN_VERSION}."
        ) from exc
    return harness_id


def _build_bundle_command(request: BundleRequest) -> list[str]:
    mars_binary = _resolve_mars_binary()
    if mars_binary is None:
        raise RuntimeError(
            "Mars launch-bundle adapter requires mars >= "
            f"{_MARS_BUNDLE_MIN_VERSION}, but no mars binary was found."
        )

    command = [
        mars_binary,
        "build",
        "launch-bundle",
        "--json",
        "--root",
        str(request.project_root),
    ]

    if request.agent:
        command.extend(["--agent", request.agent])
    if request.model_override:
        command.extend(["--model", request.model_override])
    if request.harness_override:
        command.extend(["--harness", request.harness_override])
    if request.effort_override:
        command.extend(["--effort", request.effort_override])
    if request.approval_override:
        command.extend(["--approval", request.approval_override])
    if request.sandbox_override:
        command.extend(["--sandbox", request.sandbox_override])
    for skill in request.extra_skills:
        command.extend(["--skill", skill])
    return command


def _extract_error_message(*, stdout: str, stderr: str) -> str:
    def _from_stream(raw: str) -> str | None:
        normalized = raw.strip()
        if not normalized:
            return None
        try:
            payload = json.loads(normalized)
        except (json.JSONDecodeError, ValueError):
            return normalized
        if isinstance(payload, dict):
            typed_payload = cast("dict[str, object]", payload)
            error = _normalize_str(typed_payload.get("error"))
            if error:
                return error
            message = _normalize_str(typed_payload.get("message"))
            if message:
                return message
        return normalized

    message = _from_stream(stderr) or _from_stream(stdout)
    return message or "unknown mars error"


def _raise_bundle_error(*, stdout: str, stderr: str, returncode: int | None = None) -> None:
    message = _extract_error_message(stdout=stdout, stderr=stderr)
    normalized = message.lower()
    if (
        "launch-bundle" in normalized
        and (
            "unrecognized" in normalized
            or "unknown" in normalized
            or "invalid subcommand" in normalized
        )
    ):
        raise RuntimeError(
            "Mars launch-bundle command is unavailable. Meridian requires mars >= "
            f"{_MARS_BUNDLE_MIN_VERSION}. Error: {message}"
        )
    prefix = "Mars launch-bundle failed"
    if returncode is not None:
        prefix = f"{prefix} (exit {returncode})"
    raise RuntimeError(f"{prefix}: {message}")


def _parse_bundle_payload(
    payload: dict[str, object],
    *,
    harness_registry: HarnessRegistry,
) -> _BundleResult:
    version = payload.get("version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise _bundle_schema_error("missing numeric 'version'")
    if version not in _SUPPORTED_BUNDLE_SCHEMA_VERSIONS:
        raise RuntimeError(
            "Mars launch-bundle schema version "
            f"{version} is unsupported. Expected one of {_SUPPORTED_BUNDLE_SCHEMA_VERSIONS}. "
            f"Meridian requires mars >= {_MARS_BUNDLE_MIN_VERSION}."
        )

    routing = _required_object_field(payload, "routing")
    model = _normalize_str(routing.get("model"))
    model_token = _normalize_str(routing.get("model_token")) or model
    harness = _parse_harness_id(routing.get("harness"), harness_registry=harness_registry)
    harness_model = _normalize_str(routing.get("harness_model")) or None

    execution_policy_payload = _required_object_field(payload, "execution_policy")
    execution_policy = ResolvedExecutionPolicy.model_validate(
        {
            "effort": execution_policy_payload.get("effort"),
            "approval": execution_policy_payload.get("approval"),
            "sandbox": execution_policy_payload.get("sandbox"),
            "autocompact": execution_policy_payload.get("autocompact"),
            "autocompact_pct": execution_policy_payload.get("autocompact_pct"),
            "timeout": execution_policy_payload.get("timeout"),
        }
    )

    prompt_surface = _optional_object_field(payload, "prompt_surface")
    tools = _optional_object_field(payload, "tools")

    skills_obj = _optional_object_field(payload, "skills")
    skills_loaded = _parse_skills_loaded(skills_obj.get("loaded"))
    skills_available = _parse_skills_available(skills_obj.get("available"))

    return _BundleResult(
        model=model,
        model_token=model_token,
        harness=harness,
        harness_model=harness_model,
        execution_policy=execution_policy,
        provenance=_as_str_dict(payload.get("provenance")),
        warnings=_as_str_tuple(payload.get("warnings")),
        prompt_surface_inventory_prompt=_normalize_str(prompt_surface.get("inventory_prompt")),
        tools_allowed=_as_str_tuple(tools.get("allowed")),
        tools_disallowed=_as_str_tuple(tools.get("disallowed")),
        tools_mcp=_as_str_tuple(tools.get("mcp")),
        skills_loaded=skills_loaded,
        skills_available=skills_available,
        skills_missing=_as_str_tuple(skills_obj.get("missing")),
    )


def request_and_resolve(
    request: BundleRequest,
    *,
    harness_registry: HarnessRegistry,
) -> _BundleResult:
    """Resolve launch routing and execution policy through Mars launch-bundle."""

    command = _build_bundle_command(request)
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=60,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            "Mars launch-bundle adapter requires mars >= "
            f"{_MARS_BUNDLE_MIN_VERSION}, but the mars binary was not found."
        ) from exc
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise RuntimeError(
            "Mars launch-bundle command failed to execute. Meridian requires mars >= "
            f"{_MARS_BUNDLE_MIN_VERSION}."
        ) from exc

    if result.returncode != 0:
        _raise_bundle_error(
            stdout=result.stdout,
            stderr=result.stderr,
            returncode=result.returncode,
        )

    try:
        payload_obj = json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError(
            "Mars launch-bundle returned invalid JSON output. "
            f"Meridian requires mars >= {_MARS_BUNDLE_MIN_VERSION}."
        ) from exc
    if not isinstance(payload_obj, dict):
        raise RuntimeError("Mars launch-bundle returned a non-object JSON payload.")
    payload = cast("dict[str, object]", payload_obj)
    return _parse_bundle_payload(payload, harness_registry=harness_registry)


def _map_provenance_level(value: str | None) -> ProvenanceLevel:
    normalized = (value or "").strip().lower()
    mapping = {
        "cli": ProvenanceLevel.CLI,
        "env": ProvenanceLevel.ENV,
        "overlay-model-policy": ProvenanceLevel.OVERLAY_MODEL_POLICY,
        "overlay": ProvenanceLevel.OVERLAY,
        "settings-model-policy": ProvenanceLevel.SETTINGS_MODEL_POLICY,
        "matched_policy_rule": ProvenanceLevel.MATCHED_POLICY_RULE,
        "profile-model-policy": ProvenanceLevel.PROFILE_MODEL_POLICY,
        "profile-default": ProvenanceLevel.PROFILE_DEFAULT,
        "config": ProvenanceLevel.CONFIG_DEFAULT,
        "project": ProvenanceLevel.CONFIG_DEFAULT,
        "config-default": ProvenanceLevel.CONFIG_DEFAULT,
        "provider": ProvenanceLevel.ALIAS_DEFAULT,
        "alias-default": ProvenanceLevel.ALIAS_DEFAULT,
        "unset": ProvenanceLevel.UNSET,
    }
    return mapping.get(normalized, ProvenanceLevel.UNSET)


def _provenance_value(provenance: dict[str, str], field_name: str) -> str | None:
    return provenance.get(f"{field_name}_source") or provenance.get(field_name)


def _resolved_tools_field(bundle: _BundleResult) -> ToolsField | None:
    if not bundle.tools_allowed and not bundle.tools_disallowed:
        return None
    tools: dict[str, ToolAction] = {}
    for capability in bundle.tools_allowed:
        tools[capability] = "allow"
    for capability in bundle.tools_disallowed:
        tools[capability] = "deny"
    return tools


def bundle_to_resolved_policy(
    *,
    bundle: _BundleResult,
    profile: AgentProfile | None,
    resolved_skills: ResolvedSkills,
    model_selection: ModelSelectionContext | None,
    resolved_model: str,
    resolved_harness: HarnessId,
    routing_model: str | None,
    routing_agent: str | None,
    execution_policy: ResolvedExecutionPolicy,
    warnings: tuple[CompositionWarning, ...],
    alias_catalog: dict[str, AliasEntry] | None,
    harness_registry: HarnessRegistry,
    terminal_surface_mode: TerminalSurfaceMode,
    provenance_overrides: dict[str, str] | None = None,
) -> ResolvedLaunchPolicy:
    """Map bundle facts + local composition context to ResolvedLaunchPolicy."""

    harness_id = resolved_harness
    try:
        adapter = harness_registry.get_subprocess_harness(harness_id)
    except (KeyError, TypeError) as exc:
        supported = ", ".join(str(harness) for harness in harness_registry.ids())
        raise ValueError(
            f"Unknown or unsupported harness '{harness_id}'. Available harnesses: {supported}"
        ) from exc

    from meridian.lib.launch.launch_types import ResolvedLaunchRouting
    from meridian.lib.launch.policies import ResolvedLaunchPolicy

    effective_provenance = dict(bundle.provenance)
    if provenance_overrides:
        for key, value in provenance_overrides.items():
            normalized = _normalize_str(value)
            if normalized:
                effective_provenance[key] = normalized

    return ResolvedLaunchPolicy(
        profile=profile,
        model=resolved_model,
        harness=harness_id,
        adapter=adapter,
        resolved_skills=resolved_skills,
        routing=ResolvedLaunchRouting(
            model=routing_model,
            harness=harness_id,
            agent=routing_agent,
        ),
        execution_policy=execution_policy,
        resolved_tools=_resolved_tools_field(bundle),
        resolved_mcp_tools=bundle.tools_mcp,
        terminal_surface_mode=terminal_surface_mode,
        field_provenance=FieldProvenance(
            model_source=_map_provenance_level(
                _provenance_value(effective_provenance, "model")
            ),
            harness_source=_map_provenance_level(
                _provenance_value(effective_provenance, "harness")
            ),
            effort_source=_map_provenance_level(
                _provenance_value(effective_provenance, "effort")
            ),
            approval_source=_map_provenance_level(
                _provenance_value(effective_provenance, "approval")
            ),
            sandbox_source=_map_provenance_level(
                _provenance_value(effective_provenance, "sandbox")
            ),
            autocompact_source=_map_provenance_level(
                _provenance_value(effective_provenance, "autocompact")
            ),
            autocompact_pct_source=_map_provenance_level(
                _provenance_value(effective_provenance, "autocompact_pct")
            ),
            timeout_source=_map_provenance_level(
                _provenance_value(effective_provenance, "timeout")
            ),
        ),
        matched_policy_rule=effective_provenance.get("matched_policy_rule"),
        model_selection=model_selection,
        fallback_chain=(),
        warnings=warnings,
        alias_catalog=alias_catalog,
        bundle_inventory_prompt=bundle.prompt_surface_inventory_prompt or None,
        bundle_available_skills=bundle.skills_available,
    )


__all__ = [
    "BundleRequest",
    "LoadedSkillEntry",
    "bundle_to_resolved_policy",
    "request_and_resolve",
]
