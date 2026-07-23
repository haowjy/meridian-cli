"""Primary session launch policy for the root meridian command."""

from __future__ import annotations

import shlex
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from meridian.cli.argv_normalization import validate_fork_mode
from meridian.cli.utils import missing_fork_session_error_with_discovery
from meridian.lib.core.execution_policy import ResolvedExecutionPolicy
from meridian.lib.core.launch_policy_snapshot import LaunchPolicySnapshot
from meridian.lib.core.util import FormatContext
from meridian.lib.harness.registry import get_default_harness_registry
from meridian.lib.launch import LaunchRequest, SessionMode, launch_primary
from meridian.lib.launch.composition import PromptDocument
from meridian.lib.launch.continue_replay import (
    build_continue_replay_contract,
    continue_replay_source_from_reference,
)
from meridian.lib.launch.request import SessionRequest
from meridian.lib.launch.resolve import resolve_agent_launch_input
from meridian.lib.ops.reference import ResolvedSessionReference, resolve_session_reference
from meridian.lib.ops.spawn.models import normalize_goal


def _headless_claude_startup_warning(project_root: Path) -> str | None:
    """Warn at primary startup when headless Claude spawns are NOT denied.

    Anthropic disables headless Claude runs on 2026-06-15; with `claude` absent
    from `[spawn] deny_headless_harnesses` (the default keeps it), `meridian spawn`
    of Claude agents will break. Native Claude→Claude delegation via the Agent tool
    is unaffected. Fires only when the user opted out of the default.
    """
    from meridian.lib.config.settings import load_config

    try:
        deny = load_config(project_root, resolve_models=False).deny_headless_harnesses
    except Exception:
        return None
    if "claude" in deny:
        return None
    return (
        "headless 'claude' spawns are allowed by your config. Anthropic disables "
        "headless Claude runs on 2026-06-15, which will break `meridian spawn` of "
        "Claude agents. Keep 'claude' in [spawn] deny_headless_harnesses (the "
        "default) and delegate to Claude subagents via the native Agent tool."
    )


class PrimaryLaunchOutput(BaseModel):
    model_config = ConfigDict(frozen=True)

    message: str
    exit_code: int
    command: tuple[str, ...] = ()
    continue_ref: str | None = None
    continue_chat_id: str | None = None
    forked_from: str | None = None
    resume_command: str | None = None
    warning: str | None = None
    terminal_surface_mode: str | None = None

    def format_text(self, ctx: FormatContext | None = None) -> str:
        _ = ctx
        lines: list[str] = []
        if self.warning:
            lines.append(f"warning: {self.warning}")
        if self.command:
            if self.forked_from:
                lines.append(f"{self.message} (from {self.forked_from})")
            else:
                lines.append(self.message)
            if self.terminal_surface_mode:
                lines.append(f"Terminal surface mode: {self.terminal_surface_mode}")
            lines.append(shlex.join(self.command))
            return "\n".join(lines)
        if self.resume_command:
            if self.forked_from:
                lines.append(f"Session forked from {self.forked_from}.")
            else:
                lines.append(self.message)
            lines.append("To continue with meridian:")
            lines.append(self.resume_command)
            return "\n".join(lines)
        if self.forked_from:
            lines.append(f"{self.message} (from {self.forked_from})")
        else:
            lines.append(self.message)
        return "\n".join(lines)


def resolve_session_target(
    *,
    project_root: Path,
    continue_ref: str,
) -> ResolvedSessionReference:
    normalized = continue_ref.strip()
    if not normalized:
        raise ValueError("--continue requires a non-empty session reference.")
    return resolve_session_reference(project_root, normalized)


def run_primary_launch(
    *,
    project_root: Path | None = None,
    continue_ref: str | None,
    fork_ref: str | None,
    fork_fresh_ref: str | None,
    from_ref: str | None = None,
    model: str,
    harness: str | None,
    agent: str | None,
    work: str,
    task_dir: str | None = None,
    yolo: bool,
    approval: str | None,
    autocompact: int | None,
    autocompact_pct: int | None = None,
    effort: str | None,
    sandbox: str | None,
    timeout: float | None,
    dry_run: bool,
    passthrough: tuple[str, ...],
    reference_files: tuple[str, ...] = (),
    prompt: str | None = None,
    skills: tuple[str, ...] = (),
    goal: str | None = None,
    supplemental_prompt_documents: tuple[PromptDocument, ...] = (),
    include_bootstrap_documents: bool = False,
) -> PrimaryLaunchOutput:
    def _result_message(*, exit_code: int) -> str:
        if dry_run:
            if resume_target is not None:
                return "Resume dry-run."
            if selected_fork_target is not None:
                return "Fork dry-run."
            return "Launch dry-run."
        if exit_code == 0:
            if resume_target is not None:
                return "Session resumed."
            if selected_fork_target is not None:
                return "Session forked."
            return "Session finished."
        if resume_target is not None:
            return "Session resume failed."
        if selected_fork_target is not None:
            return "Session fork failed."
        return "Session failed."

    def _merge_warnings(*warnings: str | None) -> str | None:
        parts = [item.strip() for item in warnings if item and item.strip()]
        if not parts:
            return None
        return "; ".join(parts)

    project_root = project_root.resolve() if project_root is not None else Path.cwd().resolve()
    harness_registry = get_default_harness_registry()
    normalized_continue_ref = continue_ref.strip() if continue_ref is not None else ""
    resume_target = normalized_continue_ref if normalized_continue_ref else None
    raw_fork_target = fork_ref.strip() if fork_ref is not None else ""
    raw_fork_fresh_target = fork_fresh_ref.strip() if fork_fresh_ref is not None else ""
    raw_from_target = from_ref.strip() if from_ref is not None else ""
    fork_target_requested = raw_fork_target or None
    fork_fresh_target_requested = raw_fork_fresh_target or None
    context_from_requested = (raw_from_target,) if raw_from_target else ()
    normalized_task_dir = (task_dir or "").strip() or None
    if resume_target is not None and normalized_task_dir is not None:
        raise ValueError(
            "--continue does not accept --task-dir. Use --fork --task-dir to diverge."
        )
    if resume_target is not None and work.strip():
        raise ValueError(
            "--continue does not accept --work. "
            "Use --fork-fresh or a fresh session to change work context."
        )
    resolved_approval = approval if approval is not None else ("never" if yolo else "default")

    fork_resolution = validate_fork_mode(
        fork_from=fork_target_requested,
        fork_fresh_from=fork_fresh_target_requested,
        continue_from=resume_target,
        context_from=context_from_requested,
        agent=agent,
        model=model,
        skills=",".join(skills) if skills else None,
    )
    fork_target = fork_resolution.fork_ref
    fork_fresh_target = fork_resolution.fork_fresh_ref
    selected_fork_target = fork_target if fork_target is not None else fork_fresh_target

    continue_harness_session_id: str | None = None
    continue_chat_id: str | None = None
    continue_harness: str | None = None
    continue_fork = False
    continue_warning: str | None = None
    forked_from_chat_id: str | None = None
    source_control_root: str | None = None
    source_execution_cwd: str | None = None
    source_claude_config_dir: str | None = None
    source_pi_session_dir: str | None = None
    continue_source_tracked = False
    continue_source_ref: str | None = None
    continue_launch_policy_snapshot: LaunchPolicySnapshot | None = None
    continue_passthrough_args: tuple[str, ...] = ()
    output_forked_from: str | None = None
    session_mode = SessionMode.FRESH
    explicit_harness = harness.strip() if harness is not None and harness.strip() else None
    agent_launch = resolve_agent_launch_input(agent)
    requested_model: str | None = model
    requested_agent: str | None = agent_launch.agent
    agent_opt_out = agent_launch.agent_opt_out
    requested_skills: tuple[str, ...] = skills
    requested_work_id = work.strip() or None
    launch_task_dir = normalized_task_dir
    if resume_target is not None:
        if model.strip():
            raise ValueError("Cannot combine --continue with --model.")
        if skills:
            raise ValueError("Cannot combine --continue with --skills.")
        if passthrough:
            raise ValueError("Cannot combine --continue with passthrough args (--).")
        resolved_continue = resolve_session_target(
            project_root=project_root, continue_ref=resume_target
        )
        if resolved_continue.missing_harness_session_id:
            raise ValueError(
                missing_fork_session_error_with_discovery(
                    source_ref=resume_target,
                    project_root=project_root,
                    source_harness=resolved_continue.harness,
                    source_chat_id=resolved_continue.source_chat_id,
                )
            )
        continue_contract = build_continue_replay_contract(
            source=continue_replay_source_from_reference(
                source_ref=resume_target,
                resolved_reference=resolved_continue,
                harness_session_id=resolved_continue.authoritative_harness_session_id,
            ),
            explicit_harness=explicit_harness,
            requested_agent=agent_launch.agent,
            agent_opt_out=agent_launch.agent_opt_out,
        )
        continue_harness_session_id = continue_contract.session.requested_harness_session_id
        continue_chat_id = continue_contract.session.continue_chat_id
        continue_harness = continue_contract.harness
        continue_warning = resolved_continue.warning
        source_control_root = continue_contract.session.source_control_root
        source_execution_cwd = continue_contract.session.source_execution_cwd
        source_claude_config_dir = continue_contract.session.source_claude_config_dir
        source_pi_session_dir = continue_contract.session.source_pi_session_dir
        if requested_work_id is None:
            requested_work_id = continue_contract.work_id
        launch_task_dir = continue_contract.task_dir
        if launch_task_dir is not None and not Path(launch_task_dir).exists():
            continue_warning = _merge_warnings(
                continue_warning,
                f"Continued session's task_dir no longer exists: {launch_task_dir}; "
                "falling back to the normal launch directory.",
            )
            launch_task_dir = project_root.as_posix()
        continue_source_tracked = continue_contract.session.continue_source_tracked
        continue_source_ref = continue_contract.session.continue_source_ref
        continue_launch_policy_snapshot = continue_contract.launch_policy_snapshot
        continue_passthrough_args = continue_contract.passthrough_args
        requested_model = continue_contract.model
        requested_agent = continue_contract.agent
        agent_opt_out = continue_contract.agent_opt_out
        requested_skills = continue_contract.skills
        session_mode = SessionMode.RESUME
    elif selected_fork_target is not None:
        resolved_fork = resolve_session_target(
            project_root=project_root, continue_ref=selected_fork_target
        )
        if resolved_fork.missing_harness_session_id:
            raise ValueError(
                missing_fork_session_error_with_discovery(
                    source_ref=selected_fork_target,
                    project_root=project_root,
                    source_harness=resolved_fork.harness,
                    source_chat_id=resolved_fork.source_chat_id,
                )
            )

        source_harness = (
            resolved_fork.harness.strip()
            if resolved_fork.harness is not None and resolved_fork.harness.strip()
            else None
        )
        if (
            explicit_harness is not None
            and source_harness is not None
            and explicit_harness != source_harness
        ):
            raise ValueError(
                "Cannot fork across harnesses: "
                f"source is '{source_harness}', target is '{explicit_harness}'."
            )

        continue_harness_session_id = resolved_fork.authoritative_harness_session_id
        continue_harness = explicit_harness or source_harness
        if continue_harness is None:
            missing_session_ref = (
                resolved_fork.authoritative_harness_session_id or selected_fork_target
            )
            raise ValueError(
                f"Session '{missing_session_ref}' "
                "not recognized by any harness. "
                "Use --harness to specify which harness owns this session."
            )
        continue_warning = resolved_fork.warning
        continue_fork = True
        forked_from_chat_id = resolved_fork.source_chat_id
        source_control_root = resolved_fork.source_control_root
        source_execution_cwd = resolved_fork.source_execution_cwd
        source_claude_config_dir = resolved_fork.source_claude_config_dir
        source_pi_session_dir = resolved_fork.source_pi_session_dir
        continue_source_tracked = resolved_fork.tracked
        continue_source_ref = selected_fork_target
        output_forked_from = resolved_fork.source_chat_id or selected_fork_target
        session_mode = SessionMode.FORK

        if not model.strip() and resolved_fork.source_model is not None:
            requested_model = resolved_fork.source_model
        if agent is None and not agent_opt_out and resolved_fork.source_agent is not None:
            requested_agent = resolved_fork.source_agent
        if requested_work_id is None and resolved_fork.source_work_id is not None:
            requested_work_id = resolved_fork.source_work_id

    resolved_goal = normalize_goal(goal)

    launch_result = launch_primary(
        project_root=project_root,
        request=LaunchRequest(
            model=requested_model,
            harness=(
                continue_harness
                if (resume_target is not None or selected_fork_target is not None)
                else harness
            ),
            agent=requested_agent,
            agent_opt_out=agent_opt_out,
            work_id=requested_work_id,
            task_dir=launch_task_dir,
            passthrough_args=(
                continue_passthrough_args if resume_target is not None else passthrough
            ),
            session_mode=session_mode,
            pinned_context="",
            supplemental_prompt_documents=supplemental_prompt_documents,
            include_bootstrap_documents=include_bootstrap_documents,
            context_from=fork_resolution.resolved_context_from,
            reference_files=reference_files,
            prompt=prompt,
            skills=requested_skills,
            goal=resolved_goal,
            dry_run=dry_run,
            execution_policy=ResolvedExecutionPolicy(
                approval=resolved_approval if resolved_approval != "default" else None,
                effort=effort,
                sandbox=sandbox,
                timeout=timeout,
                autocompact=autocompact,
                autocompact_pct=autocompact_pct,
            ),
            launch_policy_snapshot=continue_launch_policy_snapshot,
            session=SessionRequest(
                requested_harness_session_id=continue_harness_session_id,
                continue_harness=continue_harness,
                continue_chat_id=continue_chat_id,
                continue_fork=continue_fork,
                forked_from_chat_id=forked_from_chat_id,
                source_control_root=source_control_root,
                source_execution_cwd=source_execution_cwd,
                source_claude_config_dir=source_claude_config_dir,
                source_pi_session_dir=source_pi_session_dir,
                continue_source_tracked=continue_source_tracked,
                continue_source_ref=continue_source_ref,
            ),
        ),
        harness_registry=harness_registry,
    )

    continue_chat_id = getattr(launch_result, "continue_chat_id", None)
    return PrimaryLaunchOutput(
        message=_result_message(exit_code=launch_result.exit_code),
        exit_code=launch_result.exit_code,
        command=launch_result.command if dry_run else (),
        continue_ref=launch_result.continue_ref,
        continue_chat_id=continue_chat_id,
        forked_from=output_forked_from,
        resume_command=(
            f"meridian --continue {continue_chat_id}"
            if continue_chat_id is not None
            else (
                f"meridian --continue {launch_result.continue_ref}"
                if launch_result.continue_ref is not None
                else None
            )
        ),
        warning=_merge_warnings(
            continue_warning,
            launch_result.warning,
            _headless_claude_startup_warning(project_root),
        ),
        terminal_surface_mode=getattr(launch_result, "terminal_surface_mode", None),
    )
