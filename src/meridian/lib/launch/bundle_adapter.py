"""Mars launch-bundle adapter for spawn/chat launch policy routing.

Meridian deliberately recomposes the launch prompt from the selected local profile,
``skills.loaded``, and launch-owned context/report blocks. The bundle's precomposed
``agent_body``, ``prompt_surface.system_instruction``, ``supplemental_documents``, and
``scaffold_slots`` are therefore unread rather than competing instruction surfaces.
The top-level ``agent`` duplicates the request's selected local profile. Top-level routing
diagnostics duplicate ``route_trace`` and stay there; provenance keys without launch
semantics remain diagnostic. ``tools.rules`` is the permission contract; the legacy
``tools.allowed``, ``disallowed``, and ``mcp`` derivatives are not server config.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from meridian.lib.catalog.mars_roots import (
    MarsRoots,
    ensure_resolved_mars_root,
    mars_fallback_args,
    resolve_mars_executable,
    resolve_mars_roots,
)
from meridian.lib.core.route_report import RouteDecisionReport
from meridian.lib.core.types import HarnessId
from meridian.lib.launch.compiler import FieldProvenance, ProvenanceLevel
from meridian.lib.launch.composition import AvailableSkillEntry
from meridian.lib.launch.launch_types import (
    CompositionWarning,
    ResolvedExecutionPolicy,
    TerminalSurfaceMode,
)
from meridian.lib.launch.request import NetworkOverrides
from meridian.lib.safety.policy import (
    Allow,
    Hosts,
    NetworkPolicyInput,
    SandboxDerivedNetwork,
    canonical_host,
)
from meridian.lib.tools import PLATFORM_ORIGIN, ToolRule
from meridian.lib.utils.json_shapes import json_object

if TYPE_CHECKING:
    from meridian.lib.catalog.agent import AgentProfile
    from meridian.lib.catalog.model_aliases import AliasEntry
    from meridian.lib.harness.registry import HarnessRegistry
    from meridian.lib.launch.policies import ModelSelectionContext, ResolvedLaunchPolicy
    from meridian.lib.launch.resolve import ResolvedSkills


# First Mars release whose launch bundle carries ``execution_policy.network`` and
# ``provenance.network_source``.
_MARS_BUNDLE_MIN_VERSION = "0.18.0"
_SUPPORTED_BUNDLE_SCHEMA_VERSIONS = (4,)
# First Mars release whose bundle carries ``tools.rules`` (typed tool rules).
_MARS_RULES_MIN_VERSION = "0.17.0"


@dataclass(frozen=True)
class BundleRequest:
    """Input to launch-bundle resolution."""

    agent: str | None
    project_root: Path
    agent_path: Path | None = None
    model_override: str | None = None
    literal_model: bool = False
    harness_override: str | None = None
    excluded_harnesses: tuple[HarnessId, ...] = ()
    effort_override: str | None = None
    approval_override: str | None = None
    sandbox_override: str | None = None
    network_overrides: NetworkOverrides = field(default_factory=NetworkOverrides)
    extra_skills: tuple[str, ...] = ()
    no_refresh_models: bool = False


@dataclass(frozen=True)
class LoadedSkillEntry:
    """Structured loaded skill from the bundle (name + type + body)."""

    name: str
    skill_type: str
    body: str
    source_path: str | None = None


@dataclass(frozen=True)
class BundleResult:
    agent_source_path: str | None
    selection_report: dict[str, object]
    model: str
    model_token: str
    provider_constraint: str | None
    harness: HarnessId
    harness_model: str | None
    execution_policy: ResolvedExecutionPolicy
    provenance: dict[str, str]
    warnings: tuple[str, ...]
    prompt_surface_inventory_prompt: str
    tool_rules: tuple[ToolRule, ...]
    platform_tool_rules: tuple[ToolRule, ...]
    skills_loaded: tuple[LoadedSkillEntry, ...]
    skills_available: tuple[AvailableSkillEntry, ...]
    skills_missing: tuple[str, ...]


def _resolve_mars_binary() -> str | None:
    return resolve_mars_executable()


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
                source_path = _normalize_str(item_dict.get("source_path")) or None
                entries.append(
                    LoadedSkillEntry(
                        name=name,
                        skill_type=skill_type,
                        body=body,
                        source_path=source_path,
                    )
                )
    return tuple(entries)


def _parse_skills_available(raw: object) -> tuple[AvailableSkillEntry, ...]:
    """Parse skill names, types, descriptions, and paths from ``skills.available``."""
    if not isinstance(raw, list):
        return ()
    entries: list[AvailableSkillEntry] = []
    for item in cast("list[object]", raw):
        if isinstance(item, dict):
            item_dict = cast("dict[str, object]", item)
            name = _normalize_str(item_dict.get("name"))
            skill_type = _normalize_str(item_dict.get("skill_type")) or "reference"
            if name:
                entries.append(
                    AvailableSkillEntry(
                        name=name,
                        skill_type=skill_type,
                        description=" ".join(
                            _normalize_str(item_dict.get("description")).split()
                        ),
                        source_path=_normalize_str(item_dict.get("source_path")) or None,
                    )
                )
    return tuple(entries)


def _bundle_schema_error(message: str) -> RuntimeError:
    return RuntimeError(
        f"Mars launch-bundle returned invalid schema: {message}. "
        "Expected bundle schema 4 with routing report schema 3."
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


class _BundleNetwork(BaseModel):
    """Current Mars network object before sandbox-derived default resolution."""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["allow", "deny", "allowlist"] | None
    allow: tuple[str, ...]
    deny: tuple[str, ...]


def _parse_bundle_network(raw: object, *, sandbox: object) -> NetworkPolicyInput:
    """Parse the current Mars object once; durable legacy shapes use their own parser."""

    try:
        bundle_network = _BundleNetwork.model_validate(raw)
    except ValidationError as exc:
        raise _bundle_schema_error(f"invalid 'execution_policy.network' object: {exc}") from exc

    try:
        allow = frozenset(canonical_host(host) for host in bundle_network.allow)
        deny = frozenset(canonical_host(host) for host in bundle_network.deny)
    except ValueError as exc:
        raise _bundle_schema_error(f"invalid 'execution_policy.network' host: {exc}") from exc

    if bundle_network.mode == "deny":
        return Hosts()
    if bundle_network.mode == "allowlist":
        return Hosts(allow=allow, deny=deny)
    if bundle_network.mode == "allow":
        return Allow(deny=deny)

    normalized_sandbox = _normalize_str(sandbox) or "default"
    if normalized_sandbox not in {
        "default",
        "read-only",
        "workspace-write",
        "danger-full-access",
    }:
        raise _bundle_schema_error(
            "'execution_policy.sandbox' must be default, read-only, workspace-write, "
            "danger-full-access, or null when network.mode is null"
        )
    return SandboxDerivedNetwork(allow=allow, deny=deny)


def _parse_tool_rules(tools: dict[str, object]) -> tuple[ToolRule, ...]:
    """Read ``tools.rules``: capability for meaning, native names for emission and tools.

    The flat ``allowed``/``disallowed`` lists Mars still derives from the rules cannot carry
    ``ask``, ``*`` or the capability behind a name, so nothing here reads them.
    """

    raw_rules = tools.get("rules")
    if raw_rules is None:
        raise RuntimeError(
            "Mars launch-bundle has no tools.rules, so this Mars predates typed tool rules. "
            f"Install mars >= {_MARS_RULES_MIN_VERSION}."
        )
    if not isinstance(raw_rules, list):
        raise _bundle_schema_error("'tools.rules' must be a list")
    rules: list[ToolRule] = []
    for raw_rule in cast("list[object]", raw_rules):
        entry = json_object(raw_rule)
        if entry is None or not isinstance(entry.get("capability"), str):
            raise _bundle_schema_error("'tools.rules' entries must be objects with a capability")
        if not isinstance(entry.get("native_tools"), list):
            raise RuntimeError(
                "Mars launch-bundle tools.rules entries have no native_tools, so this Mars "
                "predates per-rule tool names. "
                f"Install mars >= {_MARS_RULES_MIN_VERSION}."
            )
        try:
            rule = ToolRule.model_validate(entry)
        except ValidationError as exc:
            raise _bundle_schema_error(f"invalid 'tools.rules' entry: {exc}") from exc
        rules.append(rule)
    return tuple(rules)


def _parse_platform_tool_rules(tools: dict[str, object]) -> tuple[ToolRule, ...]:
    """Read native names for Meridian's semantic agent/task platform denies."""

    raw_capabilities = tools.get("capability_natives")
    if raw_capabilities is None:
        raise RuntimeError(
            "Mars launch-bundle has no tools.capability_natives, so this Mars predates "
            "platform tool metadata. "
            f"Install mars >= {_MARS_RULES_MIN_VERSION}."
        )
    capabilities = json_object(raw_capabilities)
    if capabilities is None:
        raise _bundle_schema_error("'tools.capability_natives' must be an object")

    rules: list[ToolRule] = []
    for capability in ("agent", "task"):
        entry = json_object(capabilities.get(capability))
        if entry is None:
            raise _bundle_schema_error(
                f"'tools.capability_natives.{capability}' must be an object"
            )
        try:
            # A platform deny is unscoped, so the names Mars gives are the tools too.
            rule = ToolRule.model_validate(
                {
                    **entry,
                    "native_tools": entry.get("native"),
                    "capability": capability,
                    "pattern": None,
                    "action": "deny",
                    "origin": PLATFORM_ORIGIN,
                }
            )
        except ValidationError as exc:
            raise _bundle_schema_error(
                f"invalid 'tools.capability_natives.{capability}' entry: {exc}"
            ) from exc
        rules.append(rule)
    return tuple(rules)


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


def _build_bundle_command(
    request: BundleRequest,
    *,
    roots: MarsRoots | None = None,
) -> list[str]:
    mars_binary = _resolve_mars_binary()
    if mars_binary is None:
        raise RuntimeError(
            "Mars launch-bundle adapter requires mars >= "
            f"{_MARS_BUNDLE_MIN_VERSION}, but no mars binary was found."
        )

    resolved_roots = roots or resolve_mars_roots(request.project_root)
    command = [
        mars_binary,
        "build",
        "launch-bundle",
        "--json",
        "--root",
        resolved_roots.primary.as_posix(),
    ]
    command.extend(mars_fallback_args(resolved_roots, executable=mars_binary))

    if request.no_refresh_models:
        command.append("--no-refresh-models")
    if request.agent:
        command.extend(["--agent", request.agent])
    if request.model_override is not None:
        command.extend(["--model", request.model_override])
    if request.literal_model:
        command.append("--literal-model")
    if request.harness_override:
        command.extend(["--harness", request.harness_override])
    for harness in request.excluded_harnesses:
        command.extend(["--exclude-harness", harness.value])
    if request.effort_override:
        command.extend(["--effort", request.effort_override])
    if request.approval_override:
        command.extend(["--approval", request.approval_override])
    if request.sandbox_override:
        command.extend(["--sandbox", request.sandbox_override])
    if request.network_overrides.mode is not None:
        command.extend(["--network", request.network_overrides.mode])
    for host in request.network_overrides.allow:
        command.extend(["--network-allow", host])
    for host in request.network_overrides.deny:
        command.extend(["--network-deny", host])
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


def _append_error_note(message: str, note: str | None) -> str:
    return f"{message}\n{note}" if note is not None else message


def _describe_read_only_model_resolution(message: str, *, enabled: bool) -> str:
    if not enabled:
        return message
    return message.replace(
        "--no-refresh-models was passed",
        "Model resolution is read-only",
    )


class MarsSelectionError(RuntimeError):
    """A failed Mars decision with its diagnostic report retained for callers."""

    def __init__(
        self,
        code: str,
        message: str,
        report: RouteDecisionReport | None,
        *,
        note: str | None = None,
    ) -> None:
        self.code = code
        self.selection_report = report.model_dump(mode="json") if report is not None else None
        details: list[str] = []
        if report is not None:
            for attempt in report.model_attempts:
                attempt_reasons: list[str] = []
                for assessment in attempt.assessments:
                    extra = json_object(assessment.model_extra) or {}
                    reason = extra.get("reason", assessment.verdict)
                    attempt_reasons.append(f"{assessment.harness}: {reason}")
                reasons = ", ".join(attempt_reasons)
                details.append(f"{attempt.model_token}: {reasons}")
        super().__init__(_append_error_note("; ".join([message, *details]), note))


class _MarsError(BaseModel):
    model_config = ConfigDict(extra="ignore")

    code: object = "command_failed"
    message: object = "Mars launch-bundle failed"


class _MarsErrorEnvelope(BaseModel):
    model_config = ConfigDict(extra="ignore")

    error: _MarsError
    route_trace: RouteDecisionReport | None = None

    @field_validator("route_trace", mode="before")
    @classmethod
    def reject_explicit_null_route_trace(cls, value: object) -> object:
        if value is None:
            raise ValueError("route_trace must be a route decision report when present")
        return value


def _raise_bundle_error(
    *,
    stdout: str,
    stderr: str,
    returncode: int | None = None,
    note: str | None = None,
    models_readonly: bool = False,
) -> None:
    try:
        raw_payload = json.loads(stdout)
    except json.JSONDecodeError:
        raw_payload = None
    payload = json_object(raw_payload)
    if payload is not None and json_object(payload.get("error")) is not None:
        envelope = _MarsErrorEnvelope.model_validate(payload)
        raise MarsSelectionError(
            str(envelope.error.code),
            _describe_read_only_model_resolution(
                str(envelope.error.message), enabled=models_readonly
            ),
            envelope.route_trace,
            note=note,
        )
    message = _describe_read_only_model_resolution(
        _extract_error_message(stdout=stdout, stderr=stderr),
        enabled=models_readonly,
    )
    normalized = message.lower()
    if "launch-bundle" in normalized and (
        "unrecognized" in normalized
        or "unknown" in normalized
        or "invalid subcommand" in normalized
    ):
        raise RuntimeError(
            _append_error_note(
                "Mars launch-bundle command is unavailable. Meridian requires mars >= "
                f"{_MARS_BUNDLE_MIN_VERSION}. Error: {message}",
                note,
            )
        )
    prefix = "Mars launch-bundle failed"
    if returncode is not None:
        prefix = f"{prefix} (exit {returncode})"
    raise RuntimeError(_append_error_note(f"{prefix}: {message}", note))


def _fallback_agent_error_note(request: BundleRequest, roots: MarsRoots) -> str | None:
    agent = (request.agent or "").strip()
    if (
        request.agent_path is None
        or not agent
        or roots.user is None
        or not request.agent_path.is_relative_to(roots.user)
    ):
        return None
    return (
        f"note: '{agent}' comes from user packages ({roots.user}); "
        "its model aliases must resolve in this project's Mars config. "
        "Add the package here (meridian mars add <source>) or pass --model."
    )


def _parse_bundle_payload(
    payload: dict[str, object],
    *,
    harness_registry: HarnessRegistry,
    mars_executable: str,
) -> BundleResult:
    version = payload.get("version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise _bundle_schema_error("missing numeric 'version'")
    if version not in _SUPPORTED_BUNDLE_SCHEMA_VERSIONS:
        raise RuntimeError(
            "Mars launch-bundle schema version "
            f"{version} is unsupported. Expected one of {_SUPPORTED_BUNDLE_SCHEMA_VERSIONS}. "
            "Install a Mars release supporting bundle schema 4."
        )

    routing = _required_object_field(payload, "routing")
    model = _normalize_str(routing.get("model"))
    model_token = _normalize_str(routing.get("model_token")) or model
    harness = _parse_harness_id(routing.get("harness"), harness_registry=harness_registry)
    harness_model = _normalize_str(routing.get("harness_model")) or None

    report = RouteDecisionReport.model_validate(_required_object_field(routing, "route_trace"))
    if report.selected is None:
        raise _bundle_schema_error("successful bundle has no selected assessment")
    attempt = report.model_attempts[report.selected.attempt_index]
    assessment = attempt.assessments[report.selected.assessment_index]
    if (
        attempt.canonical_model != model
        or attempt.model_token != model_token
        or assessment.harness != harness.value
    ):
        raise _bundle_schema_error("selected report identity disagrees with routing")
    if model and not harness_model:
        raise _bundle_schema_error("named model is missing routing.harness_model")

    provenance = _as_str_dict(payload.get("provenance"))
    if "network_source" not in provenance:
        raise RuntimeError(
            f"Mars executable '{mars_executable}' returned a launch bundle without "
            "provenance.network_source, so the bundled Mars predates portable network "
            "policy. Upgrade Meridian (for uv tools: uv tool upgrade meridian-cli); "
            "Meridian prefers its bundled Mars over PATH."
        )
    execution_policy_payload = _required_object_field(payload, "execution_policy")
    native_config = _optional_object_field(execution_policy_payload, "native_config")
    sandbox = execution_policy_payload.get("sandbox")
    execution_policy = ResolvedExecutionPolicy.model_validate(
        {
            "effort": execution_policy_payload.get("effort"),
            "approval": execution_policy_payload.get("approval"),
            "sandbox": sandbox,
            "network": _parse_bundle_network(
                execution_policy_payload.get("network"),
                sandbox=sandbox,
            ),
            "autocompact": execution_policy_payload.get("autocompact"),
            "autocompact_pct": execution_policy_payload.get("autocompact_pct"),
            "timeout": execution_policy_payload.get("timeout"),
            "native_config": native_config,
        }
    )

    prompt_surface = _optional_object_field(payload, "prompt_surface")
    tools = _optional_object_field(payload, "tools")

    skills_obj = _optional_object_field(payload, "skills")
    skills_loaded = _parse_skills_loaded(skills_obj.get("loaded"))
    skills_available = _parse_skills_available(skills_obj.get("available"))

    return BundleResult(
        agent_source_path=_normalize_str(payload.get("agent_source_path")) or None,
        selection_report=report.model_dump(mode="json"),
        model=model,
        model_token=model_token,
        provider_constraint=_normalize_str(routing.get("provider_constraint")) or None,
        harness=harness,
        harness_model=harness_model,
        execution_policy=execution_policy,
        provenance=provenance,
        warnings=_as_str_tuple(payload.get("warnings")),
        prompt_surface_inventory_prompt=_normalize_str(prompt_surface.get("inventory_prompt")),
        tool_rules=_parse_tool_rules(tools),
        platform_tool_rules=_parse_platform_tool_rules(tools),
        skills_loaded=skills_loaded,
        skills_available=skills_available,
        skills_missing=_as_str_tuple(skills_obj.get("missing")),
    )


def request_and_resolve(
    request: BundleRequest,
    *,
    harness_registry: HarnessRegistry,
) -> BundleResult:
    """Resolve launch routing and execution policy through Mars launch-bundle."""

    roots = resolve_mars_roots(request.project_root)
    command = _build_bundle_command(request, roots=roots)
    ensure_resolved_mars_root(roots, executable=command[0])
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
            note=_fallback_agent_error_note(request, roots),
            models_readonly=request.no_refresh_models,
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
    bundle = _parse_bundle_payload(
        payload,
        harness_registry=harness_registry,
        mars_executable=command[0],
    )
    if not request.no_refresh_models:
        return bundle
    return replace(
        bundle,
        warnings=tuple(
            _describe_read_only_model_resolution(warning, enabled=True)
            for warning in bundle.warnings
        ),
    )


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


def bundle_to_resolved_policy(
    *,
    bundle: BundleResult,
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
        resolved_tool_rules=bundle.tool_rules,
        platform_tool_rules=bundle.platform_tool_rules,
        terminal_surface_mode=terminal_surface_mode,
        field_provenance=FieldProvenance(
            model_source=_map_provenance_level(_provenance_value(effective_provenance, "model")),
            harness_source=_map_provenance_level(
                _provenance_value(effective_provenance, "harness")
            ),
            effort_source=_map_provenance_level(_provenance_value(effective_provenance, "effort")),
            approval_source=_map_provenance_level(
                _provenance_value(effective_provenance, "approval")
            ),
            sandbox_source=_map_provenance_level(
                _provenance_value(effective_provenance, "sandbox")
            ),
            network_source=_map_provenance_level(
                _provenance_value(effective_provenance, "network")
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
        selection_report=bundle.selection_report,
        warnings=warnings,
        alias_catalog=alias_catalog,
        bundle_inventory_prompt=bundle.prompt_surface_inventory_prompt or None,
        bundle_available_skills=bundle.skills_available,
    )


__all__ = [
    "BundleRequest",
    "BundleResult",
    "LoadedSkillEntry",
    "bundle_to_resolved_policy",
    "request_and_resolve",
]
