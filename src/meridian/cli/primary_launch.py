"""Primary session launch policy for the root meridian command."""

from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from meridian.cli.argv_normalization import validate_fork_mode
from meridian.lib.core.execution_policy import ResolvedExecutionPolicy
from meridian.lib.core.util import FormatContext
from meridian.lib.harness.launch_types import ManagedPrimaryPreview
from meridian.lib.harness.registry import get_default_harness_registry
from meridian.lib.launch import LaunchRequest, SessionMode, launch_primary
from meridian.lib.launch.composition import PromptDocument
from meridian.lib.launch.continue_replay import (
    MODEL_OVERRIDE_WARNING,
)
from meridian.lib.launch.request import SessionRequest
from meridian.lib.launch.resolve import resolve_agent_launch_input
from meridian.lib.ops.reference import AuthorizedSourceUse, SourceUseRefused, resolve_source_use
from meridian.lib.ops.spawn.models import normalize_goal
from meridian.lib.state.paths import resolve_project_runtime_root


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
    launch_plan: ManagedPrimaryPreview | None = None
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
        if self.launch_plan:
            plan = self.launch_plan
            lines.extend(
                (self.message, "Managed primary launch (placeholders are resolved at startup):")
            )
            lines.append("Backend: " + shlex.join(plan.backend_command))
            if plan.requested_model:
                lines.append("Requested model: " + plan.requested_model)
            if plan.model:
                lines.append("Launch-local model: " + plan.model)
            lines.extend(plan.steps)
            lines.append(
                f"Bootstrap: {plan.bootstrap_method} {plan.bootstrap_path} "
                + json.dumps(plan.bootstrap_payload)
            )
            lines.append("Attach: " + shlex.join(plan.attach_command))
            return "\n".join(lines)
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


def run_primary_launch(
    *,
    project_root: Path | None = None,
    continue_ref: str | None,
    fork_ref: str | None,
    fork_fresh_ref: str | None,
    from_ref: str | None = None,
    model: str | None,
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
    if continue_ref is not None and model is not None:
        print(f"warning: {MODEL_OVERRIDE_WARNING}", file=sys.stderr)
        if not model.strip():
            raise ValueError("--model requires a non-empty model id or alias.")
    model = model or ""

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
        raise ValueError("--continue does not accept --task-dir. Use --fork --task-dir to diverge.")
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

    continue_source_ref: str | None = None
    session_mode = SessionMode.FRESH
    explicit_harness = harness.strip() if harness is not None and harness.strip() else None
    agent_launch = resolve_agent_launch_input(agent)
    agent_opt_out = agent_launch.agent_opt_out
    if resume_target is not None:
        if skills:
            raise ValueError("Cannot combine --continue with --skills.")
        if passthrough:
            raise ValueError("Cannot combine --continue with passthrough args (--).")
        source_use = resolve_source_use(
            resolve_project_runtime_root(project_root),
            "resume",
            resume_target,
            explicit_harness,
        )
        if isinstance(source_use, AuthorizedSourceUse):
            raise ValueError(
                "Tracked primary continuation is unsupported: transport_unqualified. "
                "The native TUI remains the primary surface; no RPC substitution is made."
            )
        if isinstance(source_use, SourceUseRefused):
            raise ValueError(
                f"Cannot continue source '{resume_target}': source-use authorization "
                f"refused ({source_use.reason})."
            )
        continue_source_ref = resume_target
        session_mode = SessionMode.RESUME
    elif selected_fork_target is not None:
        original_fork_ref = raw_fork_target or raw_fork_fresh_target
        source_use = resolve_source_use(
            resolve_project_runtime_root(project_root),
            "fork",
            original_fork_ref,
            explicit_harness,
        )
        if isinstance(source_use, AuthorizedSourceUse):
            raise ValueError(
                "Tracked primary fork is unsupported: transport_unqualified. "
                "The native TUI remains the primary surface; no RPC substitution is made."
            )
        if isinstance(source_use, SourceUseRefused):
            raise ValueError(
                f"Cannot fork source '{selected_fork_target}': source-use authorization "
                f"refused ({source_use.reason})."
            )
        continue_source_ref = original_fork_ref
        session_mode = SessionMode.FORK

    resolved_goal = normalize_goal(goal)

    launch_result = launch_primary(
        project_root=project_root,
        request=LaunchRequest(
            model=model,
            harness=harness,
            agent=agent_launch.agent,
            agent_opt_out=agent_opt_out,
            work_id=work.strip() or None,
            task_dir=normalized_task_dir,
            passthrough_args=passthrough,
            session_mode=session_mode,
            pinned_context="",
            supplemental_prompt_documents=supplemental_prompt_documents,
            include_bootstrap_documents=include_bootstrap_documents,
            context_from=fork_resolution.resolved_context_from,
            reference_files=reference_files,
            prompt=prompt,
            skills=skills,
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
            primary_source_ref=(continue_source_ref if session_mode != SessionMode.FRESH else None),
            primary_explicit_agent=agent is not None,
            session=SessionRequest(continue_source_ref=continue_source_ref),
        ),
        harness_registry=harness_registry,
    )

    continue_chat_id = getattr(launch_result, "continue_chat_id", None)
    history_warning = None
    if not dry_run and launch_result.primary_spawn_id:
        from meridian.lib.ops.session_archive import session_stop_maintenance

        history_warning = session_stop_maintenance(project_root, launch_result.primary_spawn_id)
    return PrimaryLaunchOutput(
        message=_result_message(exit_code=launch_result.exit_code),
        exit_code=launch_result.exit_code,
        command=launch_result.command if dry_run else (),
        launch_plan=launch_result.launch_plan if dry_run else None,
        continue_ref=launch_result.continue_ref,
        continue_chat_id=continue_chat_id,
        forked_from=(
            launch_result.primary_source_chat_id
            or (selected_fork_target if session_mode == SessionMode.FORK else None)
        ),
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
            launch_result.primary_source_warning,
            history_warning,
            launch_result.warning,
            _headless_claude_startup_warning(project_root),
        ),
        terminal_surface_mode=getattr(launch_result, "terminal_surface_mode", None),
    )
