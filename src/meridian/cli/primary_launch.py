"""Primary session launch policy for the root meridian command."""

from __future__ import annotations

import json
import os
import shlex
import sys
from contextlib import nullcontext
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from meridian.cli.argv_normalization import validate_fork_mode
from meridian.lib.core.execution_policy import ResolvedExecutionPolicy
from meridian.lib.core.launch_policy_snapshot import LaunchPolicySnapshot
from meridian.lib.core.native_identity import NativeSessionUnavailable
from meridian.lib.core.refs import parse_ref
from meridian.lib.core.util import FormatContext
from meridian.lib.harness.launch_types import ManagedPrimaryPreview
from meridian.lib.harness.registry import get_default_harness_registry
from meridian.lib.launch import LaunchRequest, SessionMode, launch_primary
from meridian.lib.launch.composition import PromptDocument
from meridian.lib.launch.continue_launch import (
    ContinueHold,
    begin_continue,
    resolve_continue_target,
)
from meridian.lib.launch.continue_replay import (
    MODEL_OVERRIDE_WARNING,
    build_continue_replay_contract,
    continue_replay_source_from_reference,
    reject_continue_policy_overrides,
)
from meridian.lib.launch.process.terminal_ownership import (
    format_launcher_diagnostics_pointer,
)
from meridian.lib.launch.request import NetworkOverrides, SessionRequest
from meridian.lib.launch.resolve import resolve_agent_launch_input
from meridian.lib.ops.reference import ResolvedSessionReference, resolve_session_reference
from meridian.lib.ops.run_boundary import run_boundary_summary
from meridian.lib.ops.spawn.context_ref import resolve_context_ref
from meridian.lib.ops.spawn.models import normalize_goal
from meridian.lib.safety.policy import EnforcementReport, format_report_lines
from meridian.lib.state import spawn_store
from meridian.lib.state.paths import resolve_project_runtime_root
from meridian.lib.state.spawn.model import SpawnRecord


class PrimaryLaunchOutput(BaseModel):
    model_config = ConfigDict(frozen=True)

    boundary_summary: str | None = None
    message: str
    exit_code: int
    command: tuple[str, ...] = ()
    permission_report: EnforcementReport = ()
    launch_plan: ManagedPrimaryPreview | None = None
    continue_ref: str | None = None
    continue_chat_id: str | None = None
    forked_from: str | None = None
    resume_command: str | None = None
    warning: str | None = None
    terminal_surface_mode: str | None = None
    launcher_diagnostics_path: Path | None = None

    def format_text(self, ctx: FormatContext | None = None) -> str:
        _ = ctx
        lines: list[str] = [self.boundary_summary] if self.boundary_summary else []
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
        elif self.command:
            if self.forked_from:
                lines.append(f"{self.message} (from {self.forked_from})")
            else:
                lines.append(self.message)
            if self.terminal_surface_mode:
                lines.append(f"Terminal surface mode: {self.terminal_surface_mode}")
            lines.append(shlex.join(self.command))
        elif self.resume_command:
            if self.forked_from:
                lines.append(f"Session forked from {self.forked_from}.")
            else:
                lines.append(self.message)
            lines.append("To continue with meridian:")
            lines.append(self.resume_command)
        else:
            if self.forked_from:
                lines.append(f"{self.message} (from {self.forked_from})")
            else:
                lines.append(self.message)
        if self.permission_report:
            lines.append("Permissions:")
            lines.extend(format_report_lines(self.permission_report))
        if self.launcher_diagnostics_path is not None:
            lines.append(format_launcher_diagnostics_pointer(self.launcher_diagnostics_path))
        return "\n".join(lines)


def resolve_session_target(
    *,
    project_root: Path,
    continue_ref: str,
    harness_hint: str | None = None,
) -> ResolvedSessionReference:
    normalized = continue_ref.strip()
    if not normalized:
        raise ValueError("--continue requires a non-empty session reference.")
    return resolve_session_reference(project_root, normalized, harness_hint=harness_hint)


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
    network: Literal["allow", "deny", "allowlist"] | None = None,
    network_allow: tuple[str, ...] = (),
    network_deny: tuple[str, ...] = (),
    timeout: float | None,
    dry_run: bool,
    ignore_direnv: bool | None = None,
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
    forked_from_history_id: UUID | None = None
    source_control_root: str | None = None
    source_execution_cwd: str | None = None
    source_native_store: str | None = None
    continue_source_tracked = False
    continue_source: SpawnRecord | None = None
    continue_source_chat_id: str | None = None
    continue_source_ref: str | None = None
    continue_launch_policy_snapshot: LaunchPolicySnapshot | None = None
    continue_passthrough_args: tuple[str, ...] = ()
    output_forked_from: str | None = None
    session_mode = SessionMode.FRESH
    continue_session = SessionRequest()
    explicit_harness = harness.strip() if harness is not None and harness.strip() else None
    agent_launch = resolve_agent_launch_input(agent)
    requested_model: str | None = model
    requested_agent: str | None = agent_launch.agent
    agent_opt_out = agent_launch.agent_opt_out
    requested_skills: tuple[str, ...] = skills
    requested_work_id = work.strip() or None
    launch_task_dir = normalized_task_dir
    # A continued conversation keeps the goal it launched with, the way a continued
    # spawn does: `--goal` overrides, its absence inherits.
    source_goal: str | None = None
    if resume_target is not None:
        reject_continue_policy_overrides(
            {
                "--skills": bool(skills),
                "--": bool(passthrough),
                "--task-dir": normalized_task_dir is not None,
            }
        )
        resolved_continue = resolve_session_target(
            project_root=project_root,
            continue_ref=resume_target,
            harness_hint=harness,
        )
        continue_source_chat_id = resolved_continue.source_chat_id
        # A chat's conversation belongs to the spawn that ran it last: settle which spawn this
        # continue reopens before anything is built, then build everything from that spawn.
        named_spawn = (
            spawn_store.get_spawn(
                resolve_project_runtime_root(project_root), resolved_continue.source_spawn_id
            )
            if resolved_continue.source_spawn_id is not None
            else None
        )
        if named_spawn is not None:
            continue_source, redirect_notice = resolve_continue_target(
                resolve_project_runtime_root(project_root),
                named_spawn,
                named_by_spawn_ref=parse_ref(resume_target).kind in {"spawn", "selector"},
                chat_id=continue_source_chat_id,
                parent_id=None,
                kind="primary",
            )
            if continue_source.id != named_spawn.id:
                resume_target = continue_source.id
                resolved_continue = resolve_session_target(
                    project_root=project_root,
                    continue_ref=resume_target,
                    harness_hint=harness,
                )
                continue_source_chat_id = resolved_continue.source_chat_id
            continue_warning = redirect_notice
        if resolved_continue.missing_harness_session_id:
            raise NativeSessionUnavailable(resume_target, "unbound")
        continue_contract = build_continue_replay_contract(
            source=continue_replay_source_from_reference(
                source_ref=resume_target,
                resolved_reference=resolved_continue,
                harness_session_id=resolved_continue.authoritative_harness_session_id,
            ),
            explicit_harness=explicit_harness,
            requested_agent=agent_launch.agent,
            agent_opt_out=agent_launch.agent_opt_out,
            requested_model_override=model.strip() or None,
            runtime_root=resolve_project_runtime_root(project_root),
        )
        continue_session = continue_contract.session
        continue_harness_session_id = continue_contract.session.requested_harness_session_id
        continue_chat_id = continue_contract.session.continue_chat_id
        continue_harness = continue_contract.harness
        continue_warning = _merge_warnings(continue_warning, resolved_continue.warning)
        source_control_root = continue_contract.session.source_control_root
        source_execution_cwd = continue_contract.session.source_execution_cwd
        source_native_store = continue_contract.session.source_native_store
        if requested_work_id is None:
            requested_work_id = continue_contract.work_id
        launch_task_dir = continue_contract.task_dir
        if launch_task_dir is not None and not Path(launch_task_dir).is_dir():
            continue_warning = _merge_warnings(
                continue_warning,
                f"Continued session's task_dir is unavailable or not a directory: "
                f"{launch_task_dir}; "
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
        source_goal = resolved_continue.source_goal
        session_mode = SessionMode.RESUME
    elif selected_fork_target is not None:
        resolved_fork = resolve_session_target(
            project_root=project_root, continue_ref=selected_fork_target
        )
        if resolved_fork.missing_harness_session_id:
            raise NativeSessionUnavailable(selected_fork_target, "unbound")

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
        forked_from_history_id = resolved_fork.source_history_id
        source_control_root = resolved_fork.source_control_root
        source_execution_cwd = resolved_fork.source_execution_cwd
        source_native_store = resolved_fork.source_native_store
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
        source_goal = resolved_fork.source_goal

    # Same resolver as the prompt context, so the persisted identity is the one the writer read.
    # (the bare-`--from` sentinel is already resolved to a spawn id in ``resolved_context_from``)
    context_sources = tuple(
        resolve_context_ref(project_root, ref) for ref in fork_resolution.resolved_context_from
    )
    context_source = context_sources[0] if context_sources else None
    resolved_goal = normalize_goal(goal) or normalize_goal(source_goal)

    hold: ContinueHold | None = None
    if continue_source is not None:
        # The continue keeps the spawn's ID: hold its launch lock from here until the runner
        # owns it, so a second terminal's continue is refused rather than racing this one.
        hold = begin_continue(
            project_root=project_root,
            runtime_root=resolve_project_runtime_root(project_root),
            target=continue_source,
            chat_id=continue_source_chat_id,
            parent_id=None,
            caller_spawn_id=os.environ.get("MERIDIAN_SPAWN_ID") or None,
            dry_run=dry_run,
        )
    with hold.slot if hold is not None else nullcontext():
        launch_result = launch_primary(
            project_root=project_root,
            hold=hold,
            request=LaunchRequest(
                ignore_direnv=ignore_direnv,
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
                context_refs=context_sources,
                reference_files=reference_files,
                prompt=prompt,
                skills=requested_skills,
                goal=resolved_goal,
                dry_run=dry_run,
                network_overrides=NetworkOverrides(
                    mode=network,
                    allow=network_allow,
                    deny=network_deny,
                ),
                execution_policy=ResolvedExecutionPolicy(
                    approval=resolved_approval if resolved_approval != "default" else None,
                    effort=effort,
                    sandbox=sandbox,
                    timeout=timeout,
                    autocompact=autocompact,
                    autocompact_pct=autocompact_pct,
                ),
                launch_policy_snapshot=continue_launch_policy_snapshot,
                session=continue_session.model_copy(
                    update={
                        "context_from_chat_id": context_source.chat_id if context_source else None,
                        "context_from_history_id": context_source.history_id
                        if context_source
                        else None,
                        "requested_harness_session_id": continue_harness_session_id,
                        "continue_harness": continue_harness,
                        "continue_chat_id": continue_chat_id,
                        "continue_fork": continue_fork,
                        "forked_from_chat_id": forked_from_chat_id,
                        "forked_from_history_id": forked_from_history_id,
                        "source_control_root": source_control_root,
                        "source_execution_cwd": source_execution_cwd,
                        "source_native_store": source_native_store,
                        "continue_source_tracked": continue_source_tracked,
                        "continue_source_ref": continue_source_ref,
                    }
                ),
            ),
            harness_registry=harness_registry,
        )

    continue_chat_id = launch_result.continue_chat_id
    row = None
    if not dry_run and launch_result.primary_spawn_id:
        runtime_root = resolve_project_runtime_root(project_root)
        row = spawn_store.get_spawn(runtime_root, launch_result.primary_spawn_id)
        if row is not None:
            continue_chat_id = row.continue_chat_id
    history_warning = None
    if not dry_run and launch_result.primary_spawn_id:
        try:
            from meridian.lib.ops.session_archive import session_stop_maintenance

            history_warning = session_stop_maintenance(project_root, launch_result.primary_spawn_id)
        except ImportError as exc:
            history_warning = f"Session close maintenance failed: {exc}"
    return PrimaryLaunchOutput(
        boundary_summary=(
            run_boundary_summary(row, include_unverified=False)
            if not dry_run and launch_result.primary_spawn_id and row
            else None
        ),
        message=_result_message(exit_code=launch_result.exit_code),
        exit_code=launch_result.exit_code,
        command=launch_result.command if dry_run else (),
        permission_report=launch_result.permission_report if dry_run else (),
        launch_plan=launch_result.launch_plan if dry_run else None,
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
            history_warning,
            launch_result.warning,
        ),
        terminal_surface_mode=getattr(launch_result, "terminal_surface_mode", None),
        launcher_diagnostics_path=launch_result.launcher_diagnostics_path,
    )
