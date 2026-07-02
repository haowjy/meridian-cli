"""Launch-policy snapshot build and replay helpers."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from meridian.lib.catalog.agent import AgentProfile
from meridian.lib.catalog.model_aliases import AliasEntry
from meridian.lib.core.domain import SkillContent
from meridian.lib.core.execution_policy import ResolvedExecutionPolicy
from meridian.lib.core.launch_policy_snapshot import LaunchPolicySnapshot
from meridian.lib.core.types import HarnessId
from meridian.lib.harness.adapter import SubprocessHarness
from meridian.lib.harness.registry import HarnessRegistry
from meridian.lib.launch.launch_types import ResolvedLaunchRouting, TerminalSurfaceMode
from meridian.lib.tools import ToolsField

from .request import SpawnRequest
from .resolve import ResolvedSkills, dedupe_skill_names, resolve_skills_from_profile


class SnapshotModelSelection(Protocol):
    """Shape needed to serialize model-selection metadata into snapshots."""

    @property
    def selected_model_token(self) -> str: ...

    @property
    def harness_model_id(self) -> str | None: ...


@dataclass(frozen=True)
class ReplayedModelSelection:
    """Model-selection context reconstructed from a persisted snapshot."""

    requested_token: str
    selected_model_token: str
    canonical_model_id: str
    harness_provenance: str
    harness_model_id: str | None = None


@dataclass(frozen=True)
class ReplayedLaunchPolicySnapshot:
    """Resolved policy fields reconstructed directly from a snapshot."""

    profile: AgentProfile | None
    model: str
    harness: HarnessId
    adapter: SubprocessHarness
    resolved_skills: ResolvedSkills
    routing: ResolvedLaunchRouting
    execution_policy: ResolvedExecutionPolicy
    resolved_tools: ToolsField | None = None
    resolved_mcp_tools: tuple[str, ...] = ()
    terminal_surface_mode: TerminalSurfaceMode = TerminalSurfaceMode.PTY_MEDIATED
    matched_policy_rule: str | None = None
    model_selection: ReplayedModelSelection | None = None
    fallback_chain: tuple[dict[str, object], ...] = ()
    alias_catalog: dict[str, AliasEntry] | None = None


def build_launch_policy_snapshot(
    request: SpawnRequest,
    *,
    model_selection: SnapshotModelSelection | None = None,
    loaded_skills: tuple[SkillContent, ...] = (),
    bundle_inventory_prompt: str | None = None,
    profile: AgentProfile | None = None,
) -> LaunchPolicySnapshot:
    """Build a durable launch-policy snapshot from a resolved request."""

    model = (request.model or "").strip()
    harness = (request.harness or "").strip()
    if not harness:
        raise ValueError("Resolved request is missing harness for launch policy snapshot.")
    resolved_skill_names = tuple(skill.name for skill in loaded_skills) or request.skills
    resolved_skill_paths = tuple(skill.path for skill in loaded_skills) or request.skill_paths
    return LaunchPolicySnapshot(
        model=model,
        harness=harness,
        agent=(request.agent or "").strip() or None,
        agent_opt_out=request.agent_opt_out,
        agent_profile=_persisted_agent_profile(profile=profile, request=request),
        skills=resolved_skill_names,
        skill_paths=resolved_skill_paths,
        loaded_skills=loaded_skills,
        execution_policy=request.execution_policy,
        tools=request.tools,
        mcp_tools=request.mcp_tools,
        extra_args=request.extra_args,
        env=request.env,
        model_selection_requested_token=request.model_selection_requested_token,
        model_selection_selected_token=(
            model_selection.selected_model_token
            if model_selection is not None
            else request.model_selection_requested_token or (request.model or "").strip() or None
        ),
        model_selection_canonical_id=request.model_selection_canonical_id,
        model_selection_harness_provenance=request.model_selection_harness_provenance,
        model_selection_harness_model_id=(
            model_selection.harness_model_id if model_selection is not None else None
        ),
        matched_policy_rule=request.matched_policy_rule,
        fallback_chain=request.fallback_chain,
        terminal_surface_mode=(
            request.terminal_surface_mode.value
            if request.terminal_surface_mode is not None
            else None
        ),
        bundle_inventory_prompt=(bundle_inventory_prompt or "").strip() or None,
    )


def replay_launch_policy_snapshot(
    *,
    snapshot: LaunchPolicySnapshot,
    project_root: Path,
    harness_registry: HarnessRegistry,
    skills_readonly: bool,
    alias_catalog: dict[str, AliasEntry],
    resolve_terminal_surface_mode: Callable[..., TerminalSurfaceMode],
    logger: logging.Logger | None = None,
) -> ReplayedLaunchPolicySnapshot:
    """Resolve policy fields directly from persisted snapshot data."""

    snapshot_harness = snapshot.harness.strip()
    if not snapshot_harness:
        raise ValueError("Launch policy snapshot is missing harness.")

    harness_id = HarnessId(snapshot_harness)
    snapshot_model = snapshot.model.strip()
    adapter = harness_registry.get_subprocess_harness(harness_id)
    snapshot_agent = (snapshot.agent or "").strip() or None
    snapshot_skill_names = (
        tuple(skill.name for skill in snapshot.loaded_skills)
        if snapshot.loaded_skills
        else dedupe_skill_names(snapshot.skills)
    )

    profile = _snapshot_profile(
        snapshot=snapshot,
        snapshot_skill_names=snapshot_skill_names,
        project_root=project_root,
    )
    resolved_skills = _snapshot_skills(
        snapshot=snapshot,
        project_root=project_root,
        skills_readonly=skills_readonly,
        snapshot_model=snapshot_model,
        harness_id=harness_id,
        snapshot_skill_names=snapshot_skill_names,
    )
    model_selection = _snapshot_model_selection(snapshot=snapshot, snapshot_model=snapshot_model)
    terminal_surface_mode = _resolve_snapshot_terminal_mode(
        snapshot=snapshot,
        harness_id=harness_id,
        resolve_terminal_surface_mode=resolve_terminal_surface_mode,
        logger=logger,
    )
    return ReplayedLaunchPolicySnapshot(
        profile=profile,
        model=snapshot_model,
        harness=harness_id,
        adapter=adapter,
        resolved_skills=resolved_skills,
        routing=ResolvedLaunchRouting(
            model=(model_selection.selected_model_token if model_selection is not None else None),
            harness=harness_id,
            agent=snapshot_agent,
        ),
        execution_policy=snapshot.execution_policy,
        resolved_tools=snapshot.tools,
        resolved_mcp_tools=snapshot.mcp_tools,
        terminal_surface_mode=terminal_surface_mode,
        matched_policy_rule=snapshot.matched_policy_rule,
        model_selection=model_selection,
        fallback_chain=snapshot.fallback_chain,
        alias_catalog=alias_catalog,
    )


def _snapshot_model_selection(
    *,
    snapshot: LaunchPolicySnapshot,
    snapshot_model: str,
) -> ReplayedModelSelection | None:
    if not snapshot_model:
        return None

    return ReplayedModelSelection(
        requested_token=snapshot.model_selection_requested_token or snapshot_model,
        selected_model_token=snapshot.model_selection_selected_token
        or snapshot.model_selection_requested_token
        or snapshot_model,
        canonical_model_id=snapshot.model_selection_canonical_id or snapshot_model,
        harness_provenance=snapshot.model_selection_harness_provenance or "snapshot",
        harness_model_id=snapshot.model_selection_harness_model_id,
    )


def _persisted_agent_profile(
    *,
    profile: AgentProfile | None,
    request: SpawnRequest,
) -> dict[str, object] | None:
    if profile is not None:
        return profile.model_dump(mode="json")

    agent = (request.agent or "").strip() or None
    if agent is None:
        return None

    metadata = request.agent_metadata
    description = (metadata.get("session_agent_description") or "").strip()
    body = metadata.get("session_agent_profile_body") or ""
    path_str = (metadata.get("session_agent_path") or "").strip()
    profile_path = (
        Path(path_str).expanduser().resolve()
        if path_str
        else Path(".mars") / "agents" / f"{agent}.md"
    )
    return AgentProfile(
        name=agent,
        description=description,
        skills=tuple(request.skills),
        body=body,
        path=profile_path,
        raw_content=body,
    ).model_dump(mode="json")


def _snapshot_profile(
    *,
    snapshot: LaunchPolicySnapshot,
    snapshot_skill_names: tuple[str, ...],
    project_root: Path,
) -> AgentProfile | None:
    if snapshot.agent_profile is None:
        return None

    profile = AgentProfile.model_validate(snapshot.agent_profile)
    updates: dict[str, object] = {"skills": snapshot_skill_names}
    # Direct-launch fallback may persist a relative profile path (no project
    # root at build time); resolve it against the project root on replay so
    # downstream consumers (e.g. session_agent_path) keep absolute-path parity.
    if not profile.path.is_absolute():
        updates["path"] = (project_root / profile.path).resolve()
    return profile.model_copy(update=updates)


def _snapshot_skills(
    *,
    snapshot: LaunchPolicySnapshot,
    project_root: Path,
    skills_readonly: bool,
    snapshot_model: str,
    harness_id: HarnessId,
    snapshot_skill_names: tuple[str, ...],
) -> ResolvedSkills:
    if snapshot.loaded_skills:
        return ResolvedSkills(
            skill_names=snapshot_skill_names,
            loaded_skills=snapshot.loaded_skills,
            missing_skills=(),
        )

    return resolve_skills_from_profile(
        profile_skills=dedupe_skill_names(snapshot.skills),
        project_root=project_root,
        readonly=skills_readonly,
        harness_id=harness_id.value,
        selected_model_token=snapshot.model_selection_requested_token or snapshot_model,
        canonical_model_id=snapshot.model_selection_canonical_id or snapshot_model,
    )


def _resolve_snapshot_terminal_mode(
    *,
    snapshot: LaunchPolicySnapshot,
    harness_id: HarnessId,
    resolve_terminal_surface_mode: Callable[..., TerminalSurfaceMode],
    logger: logging.Logger | None,
) -> TerminalSurfaceMode:
    terminal_surface_mode = resolve_terminal_surface_mode(harness_id=harness_id)
    if snapshot.terminal_surface_mode is None:
        return terminal_surface_mode

    try:
        return TerminalSurfaceMode(snapshot.terminal_surface_mode)
    except ValueError:
        if logger is not None:
            logger.warning(
                "Ignoring unsupported snapshot terminal_surface_mode '%s'.",
                snapshot.terminal_surface_mode,
            )
        return terminal_surface_mode


__all__ = [
    "ReplayedLaunchPolicySnapshot",
    "ReplayedModelSelection",
    "SnapshotModelSelection",
    "build_launch_policy_snapshot",
    "replay_launch_policy_snapshot",
]
