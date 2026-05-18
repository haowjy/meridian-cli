"""Mars launch-bundle integration for profile-based launches."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from meridian.lib.core.overrides import RuntimeOverrides
from meridian.lib.ops.mars import resolve_mars_executable
from meridian.lib.tools import ToolAction, ToolsField

SUPPORTED_LAUNCH_BUNDLE_VERSION = 1
MARS_LAUNCH_BUNDLE_TIMEOUT_SECS = 60
SCAFFOLD_SLOT_PLACEHOLDER = "###SLOT###"


class MarsLaunchBundleError(RuntimeError):
    """Raised when Mars cannot produce a usable launch bundle."""


class MarsLaunchBundleUnavailableError(MarsLaunchBundleError):
    """Raised when Mars launch-bundle is unavailable in this environment."""


class BundleRouting(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    model: str
    model_token: str | None = None
    harness: str
    harness_model: str | None = None
    harness_model_source: str | None = None
    harness_model_confidence: str | None = None


class BundleExecutionPolicy(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    effort: str | None = None
    approval: str | None = None
    sandbox: str | None = None
    autocompact: int | None = None
    autocompact_pct: int | None = None
    timeout: int | None = None
    native_config: dict[str, Any] | None = None


class BundlePromptDocument(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    kind: str = "skill"
    name: str
    content: str
    skill_type: str = "reference"
    path: str | None = None


class BundlePromptSurface(BaseModel):
    """Prompt surface emitted by Mars for launch-bundle consumers.

    In this integration slice, ``system_instruction`` is the authoritative rendered
    model-facing prompt. ``supplemental_documents`` and ``inventory_prompt`` are accepted
    as auxiliary metadata because Mars currently embeds their effective content into
    ``system_instruction``.
    """

    model_config = ConfigDict(extra="ignore", frozen=True)

    system_instruction: str
    supplemental_documents: tuple[BundlePromptDocument, ...] = ()
    inventory_prompt: str | None = None


class BundleScaffoldSlots(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)

    completion_contract: str | None = None
    context_prompt: str | None = None
    user_prompt_file: str | None = None
    context_files: str | None = None
    prior_session_context: str | None = None
    spawn_metadata: str | None = None


class BundleTools(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    allowed: tuple[str, ...] = ()
    disallowed: tuple[str, ...] = ()
    mcp: tuple[str, ...] = ()

    @field_validator("allowed", "disallowed")
    @classmethod
    def _reject_empty_tool_keys(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for name in value:
            _ensure_nonempty_bundle_tool_key(name, source="mars launch-bundle.tools")
        return value

    def to_tools_field(self) -> ToolsField | None:
        rules: dict[str, ToolAction] = {}
        for name in self.allowed:
            _ensure_nonempty_bundle_tool_key(name, source="mars launch-bundle.tools.allowed")
            rules[name] = "allow"
        for name in self.disallowed:
            _ensure_nonempty_bundle_tool_key(name, source="mars launch-bundle.tools.disallowed")
            rules[name] = "deny"
        return rules or None


def _ensure_nonempty_bundle_tool_key(name: str, *, source: str) -> None:
    if not name.strip():
        raise ValueError(f"Invalid tools key for '{source}': key must not be empty.")


class BundleSkillsMetadata(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    loaded: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()


class LaunchBundle(BaseModel):
    """Typed subset of Mars `build launch-bundle --json` consumed by Meridian."""

    model_config = ConfigDict(extra="allow", frozen=True)

    version: int
    agent: str | None = None
    routing: BundleRouting
    execution_policy: BundleExecutionPolicy = Field(default_factory=BundleExecutionPolicy)
    prompt_surface: BundlePromptSurface
    scaffold_slots: BundleScaffoldSlots = Field(default_factory=BundleScaffoldSlots)
    tools: BundleTools = Field(default_factory=BundleTools)
    skills_metadata: BundleSkillsMetadata = Field(default_factory=BundleSkillsMetadata)
    provenance: dict[str, str] = Field(default_factory=dict)
    warnings: tuple[str, ...] = ()


def _scaffold_slot_value_is_allowed(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value == SCAFFOLD_SLOT_PLACEHOLDER or value.strip() == ""
    return False


def _validate_scaffold_slots_are_unfilled(scaffold_slots: BundleScaffoldSlots) -> None:
    for slot_name in BundleScaffoldSlots.model_fields:
        slot_value = getattr(scaffold_slots, slot_name)
        if not _scaffold_slot_value_is_allowed(slot_value):
            raise MarsLaunchBundleError(
                "Mars launch-bundle returned prefilled scaffold slot "
                f"'{slot_name}'. This launch slice only accepts empty/placeholder slot content."
            )

    for slot_name, slot_value in (scaffold_slots.model_extra or {}).items():
        if not _scaffold_slot_value_is_allowed(slot_value):
            raise MarsLaunchBundleError(
                "Mars launch-bundle returned prefilled scaffold slot "
                f"'{slot_name}'. This launch slice only accepts empty/placeholder slot content."
            )


def _append_override(args: list[str], flag: str, value: str | None) -> None:
    normalized = (value or "").strip()
    if normalized:
        args.extend([flag, normalized])


def build_launch_bundle_command(
    *,
    agent: str,
    project_root: Path,
    cli_overrides: RuntimeOverrides,
    env_overrides: RuntimeOverrides,
    requested_skills: tuple[str, ...] = (),
    executable: str | None = None,
) -> tuple[str, ...]:
    """Build a Mars launch-bundle command, relaying explicit Meridian overrides."""

    mars_bin = executable or resolve_mars_executable()
    if mars_bin is None:
        raise MarsLaunchBundleUnavailableError(
            "Mars binary not found. Run 'meridian doctor' to diagnose."
        )

    effective_user_overrides = RuntimeOverrides.model_validate(
        {
            key: getattr(cli_overrides, key) or getattr(env_overrides, key)
            for key in ("model", "harness", "effort", "approval", "sandbox")
        }
    )
    args = [
        mars_bin,
        "build",
        "launch-bundle",
        "--agent",
        agent,
        "--root",
        project_root.as_posix(),
        "--json",
    ]
    _append_override(args, "--model", effective_user_overrides.model)
    _append_override(args, "--harness", effective_user_overrides.harness)
    _append_override(args, "--effort", effective_user_overrides.effort)
    _append_override(args, "--approval", effective_user_overrides.approval)
    _append_override(args, "--sandbox", effective_user_overrides.sandbox)
    for skill in requested_skills:
        _append_override(args, "--skill", skill)
    return tuple(args)


def parse_launch_bundle(raw_json: str) -> LaunchBundle:
    """Parse and validate a Mars launch bundle."""

    try:
        payload = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise MarsLaunchBundleError("Mars launch-bundle returned invalid JSON.") from exc
    if not isinstance(payload, dict):
        raise MarsLaunchBundleError("Mars launch-bundle returned non-object JSON.")
    try:
        bundle = LaunchBundle.model_validate(payload)
    except ValidationError as exc:
        raise MarsLaunchBundleError(f"Mars launch-bundle schema mismatch: {exc}") from exc
    if bundle.version > SUPPORTED_LAUNCH_BUNDLE_VERSION:
        raise MarsLaunchBundleError(
            "Mars launch-bundle schema version "
            f"{bundle.version} is newer than supported {SUPPORTED_LAUNCH_BUNDLE_VERSION}."
        )
    _validate_scaffold_slots_are_unfilled(bundle.scaffold_slots)
    return bundle


def invoke_mars_build_launch_bundle(
    *,
    agent: str,
    project_root: Path,
    cli_overrides: RuntimeOverrides,
    env_overrides: RuntimeOverrides,
    requested_skills: tuple[str, ...] = (),
) -> LaunchBundle:
    """Call Mars and return the parsed launch bundle."""

    command = build_launch_bundle_command(
        agent=agent,
        project_root=project_root,
        cli_overrides=cli_overrides,
        env_overrides=env_overrides,
        requested_skills=requested_skills,
    )
    try:
        result = subprocess.run(
            list(command),
            check=False,
            capture_output=True,
            text=True,
            timeout=MARS_LAUNCH_BUNDLE_TIMEOUT_SECS,
        )
    except FileNotFoundError as exc:
        raise MarsLaunchBundleUnavailableError(
            "Mars binary not found. Run 'meridian doctor' to diagnose."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise MarsLaunchBundleError("Mars launch-bundle timed out.") from exc
    except OSError as exc:
        raise MarsLaunchBundleError(f"Mars launch-bundle failed to start: {exc}") from exc

    if result.returncode != 0:
        message = (result.stderr or result.stdout or "").strip()
        if not message:
            message = f"mars exited with status {result.returncode}"
        if _is_launch_bundle_capability_unavailable(message):
            raise MarsLaunchBundleUnavailableError(
                "Mars binary does not support 'build launch-bundle'. "
                f"Legacy fallback will be used. Original error: {message}"
            )
        raise MarsLaunchBundleError(f"Mars launch-bundle failed: {message}")
    return parse_launch_bundle(result.stdout)


def _is_launch_bundle_capability_unavailable(message: str) -> bool:
    normalized = message.strip().lower()
    if not normalized:
        return False

    launch_bundle_markers = ("launch-bundle", "launch_bundle")
    unknown_command_markers = (
        "unknown command",
        "unknown subcommand",
        "unrecognized subcommand",
        "no such command",
    )
    if any(marker in normalized for marker in unknown_command_markers):
        if any(marker in normalized for marker in launch_bundle_markers):
            return True
        if " build" in normalized or "'build'" in normalized or '"build"' in normalized:
            return True

    if "unexpected argument" in normalized:
        bundle_flags = (
            "--json",
            "--agent",
            "--root",
            "--skill",
            "--approval",
            "--sandbox",
            "--effort",
            "--harness",
            "--model",
        )
        if any(flag in normalized for flag in bundle_flags):
            return True

    return False


__all__ = [
    "BundleExecutionPolicy",
    "BundlePromptSurface",
    "BundleRouting",
    "BundleScaffoldSlots",
    "BundleSkillsMetadata",
    "BundleTools",
    "LaunchBundle",
    "MarsLaunchBundleError",
    "MarsLaunchBundleUnavailableError",
    "build_launch_bundle_command",
    "invoke_mars_build_launch_bundle",
    "parse_launch_bundle",
]
