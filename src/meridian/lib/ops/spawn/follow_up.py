"""`spawn --fork` and `--continue`: launch from an existing spawn."""

from contextlib import nullcontext
from pathlib import Path

from meridian.lib.core.context import RuntimeContext
from meridian.lib.core.native_identity import NativeSessionUnavailable
from meridian.lib.core.refs import RefNotFound, parse_ref
from meridian.lib.core.sink import OutputSink
from meridian.lib.launch.continue_launch import (
    begin_continue,
    resolve_continue_target,
)
from meridian.lib.launch.continue_replay import (
    build_continue_replay_contract,
    continue_replay_source_from_reference,
    reject_continue_policy_overrides,
)
from meridian.lib.launch.fork import require_replayed_agent
from meridian.lib.launch.request import SessionRequest, cross_harness_continue_error
from meridian.lib.ops.reference import ResolvedSessionReference, resolve_session_reference
from meridian.lib.ops.relocate_gate import copy_checkout_note, relocate_hint
from meridian.lib.ops.runtime import (
    runtime_context,
)
from meridian.lib.state.spawn.model import SpawnRecord

from .create import (
    execute_prepared_spawn_create,
    merge_warnings,
    run_spawn_create_preparation,
    run_spawn_creation_boundary,
)
from .models import (
    SpawnActionOutput,
    SpawnContinueInput,
    SpawnCreateInput,
    SpawnForkInput,
)
from .pre_init import (
    pre_init_failed_output,
)
from .query import (
    read_spawn_row,
)
from .roots import resolve_spawn_read_authority


def _source_spawn_for_follow_up(
    payload_spawn_id: str,
    project_root: Path,
    *,
    runtime_root: Path | None = None,
    harness_hint: str | None = None,
) -> tuple[str, SpawnRecord, ResolvedSessionReference]:
    try:
        # Resolve tracked chats in their native harness first. A requested
        # harness is not evidence that the chat belongs to that harness.
        resolved_reference = resolve_session_reference(
            project_root,
            payload_spawn_id,
            runtime_root=runtime_root,
            launching="continue",
        )
    except ValueError:
        if harness_hint is None:
            raise
        # An untracked native session ID may need the hint to disambiguate it.
        resolved_reference = resolve_session_reference(
            project_root,
            payload_spawn_id,
            runtime_root=runtime_root,
            harness_hint=harness_hint,
            launching="continue",
        )
    if (
        harness_hint is not None
        and resolved_reference.harness is not None
        and resolved_reference.harness != harness_hint
    ):
        raise ValueError(
            cross_harness_continue_error(
                resolved_reference.source_chat_id,
                resolved_reference.harness,
                harness_hint,
            )
        )
    # The reference owns which spawn the session came from: a spawn ID names itself,
    # a chat its owner, a native session its recorded provenance.
    resolved_spawn_id = resolved_reference.source_spawn_id
    row = (
        read_spawn_row(project_root, resolved_spawn_id, runtime_root=runtime_root)
        if resolved_spawn_id is not None
        else None
    )
    if resolved_spawn_id is None or row is None:
        raise ValueError(
            f"Spawn '{payload_spawn_id}' not found; continuation requires retained spawn metadata"
        )
    return resolved_spawn_id, row, resolved_reference


def _reject_continue_policy_overrides(payload: SpawnContinueInput) -> None:

    reject_continue_policy_overrides(
        {
            "--skills": bool(payload.skills),
            "--approval": payload.approval is not None,
            "--sandbox": payload.sandbox is not None,
            "--network": payload.network.mode is not None,
            "--network-allow": bool(payload.network.allow),
            "--network-deny": bool(payload.network.deny),
            "--effort": payload.effort is not None,
            "--autocompact": payload.autocompact is not None,
            "--autocompact-pct": payload.autocompact_pct is not None,
            "--": bool(payload.passthrough_args),
            "--env": bool(payload.env),
            "--task-dir": payload.task_dir is not None and bool(payload.task_dir.strip()),
        }
    )


def _build_continue_create_input(
    *,
    payload: SpawnContinueInput,
    source_spawn: SpawnRecord,
    source_spawn_id: str,
    resolved_reference: ResolvedSessionReference,
    runtime_root: Path,
) -> SpawnCreateInput:
    continue_contract = build_continue_replay_contract(
        source=continue_replay_source_from_reference(
            source_spawn_id,
            resolved_reference,
            harness_session_id=resolved_reference.authoritative_harness_session_id,
        ),
        explicit_harness=(payload.harness or "").strip() or None,
        requested_agent=payload.agent,
        agent_opt_out=payload.agent_opt_out,
        fork=payload.fork,
        requested_model_override=payload.model,
        runtime_root=runtime_root,
    )
    launch_options = payload.launch_option_updates()
    launch_options.update(
        {
            "harness": continue_contract.harness,
            "passthrough_args": continue_contract.passthrough_args,
        }
    )

    resolved_goal = payload.goal if payload.goal is not None else source_spawn.goal

    return SpawnCreateInput(
        prompt=payload.prompt,
        model=continue_contract.model,
        files=payload.files,
        template_vars=payload.template_vars,
        agent=continue_contract.agent,
        agent_opt_out=continue_contract.agent_opt_out,
        skills=continue_contract.skills,
        goal=resolved_goal,
        desc=payload.desc,
        work=payload.work.strip() or continue_contract.work_id or "",
        task_dir=continue_contract.task_dir,
        caller_cwd=payload.caller_cwd,
        launch_policy_snapshot=continue_contract.launch_policy_snapshot,
        session=continue_contract.session,
        **launch_options,
    )


def _gone_source_task_dir(
    create_input: SpawnCreateInput,
    *,
    runtime_root: Path,
    project_root: Path,
    chat_id: str | None,
) -> SpawnActionOutput | None:
    """The refusal for a continue or fork whose source's task dir is gone, with the way
    to relocate. It is the failure the launch would raise, reached before preparation."""
    task_dir = create_input.task_dir
    if not task_dir or chat_id is None or Path(task_dir).is_dir():
        return None
    message = f"task_dir does not exist: {task_dir}"
    hint = relocate_hint(runtime_root=runtime_root, chat_id=chat_id, target=project_root)
    return pre_init_failed_output(
        payload=create_input, exc=ValueError(f"{message}\n{hint}" if hint else message)
    )


def _with_command(result: SpawnActionOutput, command: str) -> SpawnActionOutput:
    return result.model_copy(update={"command": command})


def _resolve_fork_agent(
    *,
    payload: SpawnForkInput,
    requested_agent: str | None,
    source_agent: str | None,
) -> str | None:
    if payload.agent_opt_out:
        return None
    if requested_agent is not None:
        return requested_agent
    return source_agent


def _build_fork_create_input(
    *,
    payload: SpawnForkInput,
    normalized_source_ref: str,
    resolved_reference: ResolvedSessionReference,
    requested_model: str,
    requested_agent: str | None,
    inherited_skills: tuple[str, ...],
    requested_work: str,
    requested_task_dir: str | None,
    requested_goal: str | None,
    harness: str | None,
) -> SpawnCreateInput:
    launch_options = payload.launch_option_updates()
    launch_options["harness"] = harness
    return SpawnCreateInput(
        prompt=payload.prompt,
        model=requested_model or (resolved_reference.source_model or ""),
        files=payload.files,
        template_vars=payload.template_vars,
        agent=_resolve_fork_agent(
            payload=payload,
            requested_agent=requested_agent,
            source_agent=resolved_reference.source_agent,
        ),
        agent_opt_out=payload.agent_opt_out,
        skills=inherited_skills,
        desc=payload.desc,
        work=requested_work or (resolved_reference.source_work_id or ""),
        # A fork keeps the source's checkout the way an exact continue does: without
        # it the child silently edits the control root instead of the source's worktree.
        task_dir=requested_task_dir or resolved_reference.source_execution_cwd,
        caller_cwd=payload.caller_cwd,
        goal=requested_goal,
        session=SessionRequest(
            requested_harness_session_id=resolved_reference.authoritative_harness_session_id,
            continue_harness=resolved_reference.harness,
            continue_source_tracked=resolved_reference.tracked,
            continue_source_ref=normalized_source_ref,
            continue_fork=True,
            forked_from_chat_id=resolved_reference.source_chat_id,
            source_control_root=resolved_reference.source_control_root,
            source_execution_cwd=resolved_reference.source_execution_cwd,
            source_native_store=resolved_reference.source_native_store,
        ),
        **launch_options,
    )


def spawn_fork_sync(
    payload: SpawnForkInput,
    ctx: RuntimeContext | None = None,
    *,
    sink: OutputSink | None = None,
) -> SpawnActionOutput:
    floor_check = run_spawn_creation_boundary(payload)
    if isinstance(floor_check, SpawnActionOutput):
        return floor_check
    project_root, runtime_root = resolve_spawn_read_authority(
        project_root=payload.project_root,
    )
    normalized_source_ref = payload.source_ref.strip()
    if not normalized_source_ref:
        raise ValueError("Session reference is required.")
    if runtime_root is None:
        raise ValueError(f"Session reference '{normalized_source_ref}' not found")

    resolved_reference = resolve_session_reference(
        project_root,
        normalized_source_ref,
        runtime_root=runtime_root,
        launching="fork",
    )
    source_snapshot = resolved_reference.source_launch_policy_snapshot
    source_check = run_spawn_creation_boundary(
        payload,
        existing_check=floor_check,
        replayed_passthrough_args=(
            source_snapshot.extra_args if source_snapshot is not None else ()
        ),
    )
    if isinstance(source_check, SpawnActionOutput):
        return source_check
    floor_check = source_check
    if resolved_reference.missing_harness_session_id:
        raise NativeSessionUnavailable(normalized_source_ref, "unbound")

    requested_model = payload.model.strip()
    requested_agent = payload.agent
    requested_work = payload.work.strip()
    requested_task_dir = (payload.task_dir or "").strip() or None
    requested_goal = payload.goal if payload.goal is not None else resolved_reference.source_goal
    requested_harness = (payload.harness or "").strip() or None
    source_harness = (resolved_reference.harness or "").strip() or None

    inherited_skills = (
        resolved_reference.source_skills
        if payload.inherit_source_skills and requested_agent is None and not payload.agent_opt_out
        else payload.skills
    )

    unresolved_create_input = _build_fork_create_input(
        payload=payload,
        normalized_source_ref=normalized_source_ref,
        resolved_reference=resolved_reference,
        requested_model=requested_model,
        requested_agent=requested_agent,
        inherited_skills=inherited_skills,
        requested_work=requested_work,
        requested_task_dir=requested_task_dir,
        requested_goal=requested_goal,
        harness=requested_harness,
    )
    create_input = unresolved_create_input.model_copy(
        update={"project_root": project_root.as_posix()},
    )
    gone = (
        _gone_source_task_dir(
            create_input,
            runtime_root=runtime_root,
            project_root=project_root,
            chat_id=resolved_reference.source_chat_id,
        )
        if requested_task_dir is None
        else None
    )
    if gone is not None:
        return gone
    if requested_agent is None and not payload.agent_opt_out and resolved_reference.source_agent:
        try:
            require_replayed_agent(
                project_root=project_root,
                agent=resolved_reference.source_agent,
                source_ref=normalized_source_ref,
                command="meridian spawn",
            )
        except (FileNotFoundError, ValueError) as exc:
            return pre_init_failed_output(payload=create_input, exc=exc)
    preparation = run_spawn_create_preparation(
        create_input,
        ctx=ctx,
        sink=sink,
        floor_check=floor_check,
    )
    if isinstance(preparation, SpawnActionOutput):
        return preparation
    artifacts = preparation.artifacts
    if artifacts is None:
        raise RuntimeError("Fork preparation did not produce launch artifacts.")
    target_harness = (artifacts.request.harness or "").strip()
    if not target_harness:
        raise ValueError("Fork target harness could not be resolved.")
    if source_harness is not None and source_harness != target_harness:
        raise ValueError(
            "Cannot fork across harnesses: "
            f"source is '{source_harness}', target is '{target_harness}'."
        )

    return execute_prepared_spawn_create(
        preparation,
        sink=sink,
    )


def spawn_continue_sync(
    payload: SpawnContinueInput,
    ctx: RuntimeContext | None = None,
    *,
    sink: OutputSink | None = None,
) -> SpawnActionOutput:
    # Resending the source's original prompt would redo its task, so a continue states
    # what to do next. Checked first so no launch work happens for a bare continue.
    if not payload.prompt.strip():
        raise ValueError(
            f"spawn --continue {payload.spawn_id.strip()} needs a prompt: pass -p/--prompt "
            "or --prompt-file (use '-' for stdin) with the follow-up to send"
        )
    floor_check = run_spawn_creation_boundary(payload)
    if isinstance(floor_check, SpawnActionOutput):
        return _with_command(floor_check, "spawn.continue")
    if payload.model is not None and not payload.model.strip():
        raise ValueError("--model must not be empty")
    project_root, runtime_root = resolve_spawn_read_authority(
        project_root=payload.project_root,
    )
    if runtime_root is None:
        normalized = payload.spawn_id.strip()
        raise RefNotFound(normalized, "spawn")
    resolved_spawn_id, source_spawn, resolved_reference = _source_spawn_for_follow_up(
        payload.spawn_id,
        project_root,
        runtime_root=runtime_root,
        harness_hint=payload.harness,
    )
    # A chat's conversation belongs to the spawn that ran it last: settle which spawn this
    # continue reopens before anything is built, then build everything from that spawn.
    caller_spawn_id = str(runtime_context(ctx).caller_in(runtime_root) or "") or None
    target, redirect_notice = resolve_continue_target(
        runtime_root,
        source_spawn,
        named_by_spawn_ref=parse_ref(payload.spawn_id.strip()).kind in {"spawn", "selector"},
        chat_id=resolved_reference.source_chat_id,
        parent_id=caller_spawn_id,
        kind="child",
    )
    if target.id != source_spawn.id:
        resolved_spawn_id, source_spawn, resolved_reference = _source_spawn_for_follow_up(
            target.id,
            project_root,
            runtime_root=runtime_root,
            harness_hint=payload.harness,
        )
    source_snapshot = resolved_reference.source_launch_policy_snapshot
    source_check = run_spawn_creation_boundary(
        payload,
        existing_check=floor_check,
        replayed_passthrough_args=(
            source_snapshot.extra_args if source_snapshot is not None else ()
        ),
    )
    if isinstance(source_check, SpawnActionOutput):
        return _with_command(source_check, "spawn.continue")
    floor_check = source_check
    if resolved_reference.missing_harness_session_id:
        raise NativeSessionUnavailable(resolved_spawn_id, "unbound")

    _reject_continue_policy_overrides(payload)
    create_input = _build_continue_create_input(
        payload=payload,
        source_spawn=source_spawn,
        source_spawn_id=resolved_spawn_id,
        resolved_reference=resolved_reference,
        runtime_root=runtime_root,
    )
    gone = _gone_source_task_dir(
        create_input,
        runtime_root=runtime_root,
        project_root=project_root,
        chat_id=resolved_reference.source_chat_id,
    )
    if gone is not None:
        return _with_command(gone, "spawn.continue")
    redirect_notice = merge_warnings(
        redirect_notice,
        copy_checkout_note(
            runtime_root=runtime_root,
            chat_id=resolved_reference.source_chat_id,
            project_root=project_root,
        ),
    )
    preparation = run_spawn_create_preparation(
        create_input,
        ctx=ctx,
        sink=sink,
        floor_check=floor_check,
    )
    if isinstance(preparation, SpawnActionOutput):
        return _with_command(preparation, "spawn.continue")
    if preparation.depth_exceeded is not None:
        return _with_command(preparation.depth_exceeded, "spawn.continue")
    hold = begin_continue(
        project_root=project_root,
        runtime_root=runtime_root,
        target=source_spawn,
        chat_id=resolved_reference.source_chat_id,
        parent_id=caller_spawn_id,
        caller_spawn_id=caller_spawn_id,
        dry_run=payload.dry_run,
    )
    with hold.slot if hold is not None else nullcontext():
        result = execute_prepared_spawn_create(preparation, sink=sink, hold=hold)
    if redirect_notice:
        result = result.model_copy(
            update={"warning": merge_warnings(result.warning, redirect_notice)}
        )
    return _with_command(result, "spawn.continue")
