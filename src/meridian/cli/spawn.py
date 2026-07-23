"""CLI command handlers for spawn.* operations."""

import asyncio
import os
import re
import sys
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import Annotated, Any

import structlog
from cyclopts import App, Parameter

from meridian.cli.argv_normalization import validate_fork_mode
from meridian.cli.ext_registration import register_extension_cli_group
from meridian.cli.help_tiers import ADVANCED_PARAMS
from meridian.cli.spawn_inject import inject_message
from meridian.cli.utils import (
    parse_csv_list,
    require_established_project_root,
)
from meridian.lib.bootstrap.services import (
    RuntimeReadContext,
    RuntimeWriteContext,
    prepare_for_runtime_read,
    prepare_for_runtime_write,
)
from meridian.lib.core.context import RuntimeContext
from meridian.lib.core.domain import ALL_SPAWN_STATUSES, SpawnStatus
from meridian.lib.core.spawn_lifecycle import ACTIVE_SPAWN_STATUSES
from meridian.lib.core.util import FormatContext
from meridian.lib.extensions.registry import get_first_party_registry
from meridian.lib.launch.resolve import resolve_agent_launch_input
from meridian.lib.ops.spawn.api import (
    SpawnActionOutput,
    SpawnCancelAllInput,
    SpawnCancelInput,
    SpawnChildrenInput,
    SpawnContinueInput,
    SpawnCreateInput,
    SpawnForkInput,
    SpawnListInput,
    SpawnShowInput,
    SpawnSignalInput,
    SpawnStatsInput,
    SpawnStatusInput,
    SpawnSubagentsInput,
    SpawnWaitInput,
    SpawnWrittenFilesInput,
    spawn_cancel_all_sync,
    spawn_cancel_sync,
    spawn_children_sync,
    spawn_continue_sync,
    spawn_create_sync,
    spawn_done_sync,
    spawn_files_sync,
    spawn_fork_sync,
    spawn_list_sync,
    spawn_rearm_sync,
    spawn_show_sync,
    spawn_stats_sync,
    spawn_status_sync,
    spawn_subagents_sync,
    spawn_wait_sync,
)
from meridian.lib.ops.spawn.models import SpawnLaunchOptionUpdates, normalize_goal

Emitter = Callable[[Any], None]
logger = structlog.get_logger(__name__)
_SPAWN_STATUS_VALUES = tuple(ALL_SPAWN_STATUSES)
_ACTIVE_VIEW_STATUSES: tuple[SpawnStatus, ...] = tuple(
    status for status in _SPAWN_STATUS_VALUES if status in ACTIVE_SPAWN_STATUSES
)
_SPAWN_USAGE_EPILOGUE = (
    "Launch detached, then track:\n\n"
    "  meridian spawn --prompt-file PATH --bg    Detach; returns a spawn id\n"
    "  meridian spawn wait                       Track this session's pending spawns\n\n"
    "Do not block-foreground a long spawn — the harness command timeout will kill\n"
    "your shell while the spawn keeps running and you lose the thread.\n\n"
    "Harness command lifecycle ≠ meridian spawn lifecycle. The launch command\n"
    "returning or dying does not stop the spawn.\n\n"
    "If the launch was killed, reconcile with `meridian spawn wait` or\n"
    "`meridian status`.\n"
)
_SPAWN_WAIT_HELP_EPILOGUE = (
    "Without spawn ids, discovers your session's pending spawns.\n"
    "Exits cleanly at a safe checkpoint;\n"
    "re-invoke to keep waiting.\n\n"
    "Examples:\n\n"
    "  meridian spawn wait\n\n"
    "  meridian spawn wait p123\n"
)


def _get_global_options() -> Any:
    from meridian.cli.main import get_global_options

    return get_global_options()


def _current_output_sink() -> Any:
    from meridian.cli.main import current_output_sink

    return current_output_sink()


def _prepare_spawn_runtime_read() -> RuntimeReadContext:
    return prepare_for_runtime_read(require_established_project_root())


def _prepare_spawn_runtime_write() -> RuntimeWriteContext:
    logger.debug("spawn_launcher_phase", phase="bootstrap", launcher_pid=os.getpid())
    prepared = prepare_for_runtime_write(require_established_project_root())
    logger.debug(
        "spawn_launcher_phase",
        phase="bootstrap_complete",
        launcher_pid=os.getpid(),
    )
    return prepared


def _foreground_spawn_id_notifier(*, quiet: bool) -> Callable[[str], None] | None:
    if quiet or _get_global_options().output.format == "json":
        return None
    sink = _current_output_sink()

    def _notify(spawn_id: str) -> None:
        sink.status(f"Spawn id: {spawn_id}")
        from meridian.cli.output import flush_sink

        flush_sink(sink)

    return _notify


def _spawn_create_exit_code(result: SpawnActionOutput) -> int:
    if result.exit_code is not None:
        return result.exit_code
    if result.status in {"succeeded", "running", "finalizing", "dry-run"}:
        return 0
    return 1


def _read_prompt_from_stdin() -> str:
    if sys.stdin.isatty():
        raise ValueError("--prompt-file - requires stdin to be piped or redirected")
    try:
        prompt_text = sys.stdin.read()
    except UnicodeDecodeError as exc:
        raise ValueError("prompt stdin is not valid UTF-8") from exc
    if not prompt_text:
        raise ValueError("prompt stdin is empty")
    return prompt_text


def _read_prompt_from_file(prompt_file: str) -> str:
    if not prompt_file.strip():
        raise ValueError("prompt file path is empty")
    prompt_path = Path(prompt_file)
    try:
        prompt_text = prompt_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ValueError(f"prompt file not found: {prompt_file}") from exc
    except UnicodeDecodeError as exc:
        raise ValueError(f"prompt file is not valid UTF-8: {prompt_file}") from exc
    if not prompt_text:
        raise ValueError(f"prompt file is empty: {prompt_file}")
    return prompt_text


def _resolve_spawn_prompt(
    prompt: str | None,
    prompt_file: str | None,
    *,
    has_files: bool,
    is_continue: bool,
) -> str:
    if prompt is not None and prompt_file is not None:
        raise ValueError("cannot specify both -p and --prompt-file")
    if prompt is not None:
        return prompt
    if prompt_file is not None:
        if prompt_file == "-":
            return _read_prompt_from_stdin()
        return _read_prompt_from_file(prompt_file)
    if has_files or is_continue:
        return ""
    raise ValueError("prompt required: pass -p/--prompt or --prompt-file")


def _edit_distance(left: str, right: str) -> int:
    """Return the adjacent-transposition-aware edit distance for two CLI tokens."""
    distances = [[0] * (len(right) + 1) for _ in range(len(left) + 1)]
    for left_index in range(len(left) + 1):
        distances[left_index][0] = left_index
    for right_index in range(len(right) + 1):
        distances[0][right_index] = right_index

    for left_index, left_char in enumerate(left, start=1):
        for right_index, right_char in enumerate(right, start=1):
            distances[left_index][right_index] = min(
                distances[left_index - 1][right_index] + 1,
                distances[left_index][right_index - 1] + 1,
                distances[left_index - 1][right_index - 1]
                + (left_char != right_char),
            )
            if (
                left_index > 1
                and right_index > 1
                and left_char == right[right_index - 2]
                and left[left_index - 2] == right_char
            ):
                distances[left_index][right_index] = min(
                    distances[left_index][right_index],
                    distances[left_index - 2][right_index - 2] + 1,
                )
    return distances[-1][-1]


def _validate_positional_spawn_prompt(
    prompt: str | None,
    *,
    subcommands: frozenset[str],
) -> None:
    if (
        prompt is None
        or prompt in subcommands
        or re.fullmatch(r"[a-z][a-z-]*", prompt) is None
    ):
        return
    closest = min(subcommands, key=lambda command: (_edit_distance(prompt, command), command))
    suggestion = (
        f"; did you mean 'meridian spawn {closest}'?"
        if _edit_distance(prompt, closest) <= 2
        else ""
    )
    raise ValueError(
        f"unknown spawn subcommand '{prompt}'{suggestion}\n"
        "To force a literal one-word prompt, use --prompt."
    )


def _shared_launch_input_kwargs(
    *,
    dry_run: bool,
    verbose: bool,
    quiet: bool,
    stream: bool,
    background: bool,
    project_root: str | None,
    timeout: float | None,
    resident_rearm_budget: int | None,
    approval: str | None,
    autocompact: int | None,
    autocompact_pct: int | None,
    effort: str | None,
    sandbox: str | None,
    harness: str | None,
    passthrough_args: tuple[str, ...],
    debug: bool,
    task_ping_interval: str | None,
    task_ping_reset_on_activity: bool | None,
    env: tuple[str, ...],
) -> SpawnLaunchOptionUpdates:
    return {
        "dry_run": dry_run,
        "verbose": verbose,
        "quiet": quiet,
        "stream": stream,
        "background": background,
        "project_root": project_root,
        "timeout": timeout,
        "resident_rearm_budget": resident_rearm_budget,
        "approval": approval,
        "autocompact": autocompact,
        "autocompact_pct": autocompact_pct,
        "effort": effort,
        "sandbox": sandbox,
        "harness": harness,
        "passthrough_args": passthrough_args,
        "debug": debug,
        "task_ping_interval": task_ping_interval,
        "task_ping_reset_on_activity": task_ping_reset_on_activity,
        "env": env,
    }


def _spawn_create(
    subcommands: frozenset[str],
    emit: Any,
    positional_prompt: Annotated[
        str | None,
        Parameter(
            name="prompt",
            help="Inline literal prompt.",
        ),
    ] = None,
    *,
    prompt: Annotated[
        str | None,
        Parameter(
            name=["--prompt", "-p"],
            help="Inline literal prompt. Prefer --prompt-file for delegation.",
        ),
    ] = None,
    prompt_file: Annotated[
        str | None,
        Parameter(
            name="--prompt-file",
            help=("Read prompt text from a file. Preferred for delegation. Use '-' to read stdin."),
            allow_leading_hyphen=True,
        ),
    ] = None,
    template_vars: Annotated[
        tuple[str, ...],
        Parameter(
            name="--prompt-var",
            group=ADVANCED_PARAMS,
            help=(
                "Prompt template variables in KEY=VALUE form (repeatable). "
                "Replaces double-curly {{KEY}} placeholders in the prompt file "
                "at launch."
            ),
            negative_iterable=(),
        ),
    ] = (),
    model: Annotated[
        str,
        Parameter(
            name=["--model", "-m"],
            help="Model id or alias. Overrides agent profile and config defaults.",
        ),
    ] = "",
    references: Annotated[
        tuple[str, ...],
        Parameter(
            name=["--file", "-f"],
            help=("Attach reference files to the spawn (repeatable). Does not supply prompt text."),
            negative_iterable=(),
        ),
    ] = (),
    context_from: Annotated[
        tuple[str, ...],
        Parameter(
            name=["--from"],
            help=(
                "Inherit context from a prior spawn or chat/session (repeatable). "
                "Also inherits the source's work item when neither --work nor the "
                "ambient session provides one."
            ),
            negative_iterable=(),
        ),
    ] = (),
    agent: Annotated[
        str | None,
        Parameter(
            name=["--agent", "-a"],
            help='Agent profile name to execute. Use -a "" for no profile.',
        ),
    ] = None,
    skills: Annotated[
        str | None,
        Parameter(
            name=["--skills", "-s"],
            group=ADVANCED_PARAMS,
            help="Comma-separated ad-hoc skills to load. Merged with agent profile skills.",
        ),
    ] = None,
    desc: Annotated[
        str,
        Parameter(
            name=["--desc", "--description"],
            group=ADVANCED_PARAMS,
            help="Short description for the spawn.",
        ),
    ] = "",
    goal: Annotated[
        str | None,
        Parameter(
            name="--goal",
            help=(
                "Completion goal for this spawn. Injected as a bounded completion contract."
            ),
        ),
    ] = None,
    work: Annotated[
        str,
        Parameter(
            name="--work",
            help=(
                "Associate the spawn with a work item id. Takes precedence\n"
                "over ambient session work and --from inheritance."
            ),
        ),
    ] = "",
    task_dir: Annotated[
        str | None,
        Parameter(
            name="--task-dir",
            help=(
                "Override the source-code edit directory for this spawn only. "
                "Does not modify the work item's task_dir setting "
                "(check with `meridian task-dir`; rebind with "
                "`meridian work task-dir <path>`). "
                "Relative -f paths resolve against this directory."
            ),
        ),
    ] = None,
    dry_run: Annotated[
        bool,
        Parameter(
            name="--dry-run", group=ADVANCED_PARAMS, help="Preview without executing harness."
        ),
    ] = False,
    verbose: Annotated[
        bool,
        Parameter(
            name="--verbose",
            group=ADVANCED_PARAMS,
            help="Enable verbose spawn logging.",
        ),
    ] = False,
    metadata: Annotated[
        bool,
        Parameter(
            name="--metadata",
            group=ADVANCED_PARAMS,
            help=(
                "Include detailed spawn metadata in text output "
                "(report-first view remains primary)."
            ),
        ),
    ] = False,
    quiet: Annotated[
        bool,
        Parameter(
            name="--quiet",
            group=ADVANCED_PARAMS,
            help="Reduce non-essential command output.",
        ),
    ] = False,
    stream: Annotated[
        bool,
        Parameter(
            name="--stream", help="Stream raw harness output to terminal (debug only).", show=False
        ),
    ] = False,
    background: Annotated[
        bool,
        Parameter(
            name=["--background", "--bg"],
            help=(
                "Run detached; returns a spawn id without waiting. NEVER wrap "
                "`meridian spawn --bg` inside the harness's own background "
                "execution (e.g. run_in_background Bash) — it already detaches, "
                "and double-wrapping can kill the launcher before it hands off "
                "to the detached worker (the spawn then fails visibly without "
                "running)."
            ),
        ),
    ] = False,
    timeout: Annotated[
        float | None,
        Parameter(
            name="--timeout",
            group=ADVANCED_PARAMS,
            help="Maximum runtime in minutes before spawn timeout.",
        ),
    ] = None,
    resident_rearm_budget: Annotated[
        int | None,
        Parameter(
            name="--resident-rearm-budget",
            group=ADVANCED_PARAMS,
            help="Maximum resident deadline extensions; unlimited when omitted.",
        ),
    ] = None,
    approval: Annotated[
        str | None,
        Parameter(
            name="--approval",
            group=ADVANCED_PARAMS,
            help="Approval mode: default, confirm, auto, never. Overrides agent profile.",
        ),
    ] = None,
    autocompact: Annotated[
        int | None,
        Parameter(
            name="--autocompact",
            group=ADVANCED_PARAMS,
            help="Autocompact token threshold (minimum 1000). Overrides agent profile.",
        ),
    ] = None,
    autocompact_pct: Annotated[
        int | None,
        Parameter(
            name="--autocompact-pct",
            group=ADVANCED_PARAMS,
            help="Percentage of context window for autocompact (1-100). Overrides agent profile.",
        ),
    ] = None,
    effort: Annotated[
        str | None,
        Parameter(
            name="--effort",
            group=ADVANCED_PARAMS,
            help="Effort level: low, medium, high, xhigh, max. Overrides agent profile.",
        ),
    ] = None,
    sandbox: Annotated[
        str | None,
        Parameter(
            name="--sandbox",
            group=ADVANCED_PARAMS,
            help=(
                "Sandbox mode passed to harness "
                "(e.g., read-only, workspace-write). Overrides agent profile."
            ),
        ),
    ] = None,
    yolo: Annotated[
        bool,
        Parameter(
            name="--yolo",
            group=ADVANCED_PARAMS,
            help="Skip all harness safety prompts and sandboxing.",
        ),
    ] = False,
    continue_from: Annotated[
        str | None,
        Parameter(name="--continue", help="Continue from a previous spawn ID."),
    ] = None,
    fork_from: Annotated[
        str | None,
        Parameter(
            name="--fork",
            group=ADVANCED_PARAMS,
            help=(
                "Fork from a session ref while preserving launch identity "
                "(agent/model/skills): chat id (c123), spawn id (p123), or raw "
                "harness session id."
            ),
        ),
    ] = None,
    fork_fresh_from: Annotated[
        str | None,
        Parameter(
            name="--fork-fresh",
            group=ADVANCED_PARAMS,
            help=(
                "Fork from a session ref and allow launch identity changes "
                "(agent/model/skills). This may reduce prompt-cache locality."
            ),
        ),
    ] = None,
    task_ping_interval: Annotated[
        str | None,
        Parameter(
            name="--task-ping-interval",
            group=ADVANCED_PARAMS,
            help=(
                "Default background-task ping interval for spawned Pi "
                "(e.g. 90m, 1h, 3300). Overrides the 55m extension default."
            ),
        ),
    ] = None,
    task_ping_reset_on_activity: Annotated[
        bool | None,
        Parameter(
            name="--task-ping-reset-on-activity",
            group=ADVANCED_PARAMS,
            help=(
                "When true, reset each task's ping deadline on log output or "
                "task tool touch (default true when unset)."
            ),
        ),
    ] = None,
    debug: Annotated[
        bool,
        Parameter(
            name="--debug",
            help="Enable wire-level debug tracing to debug.jsonl.",
            show=False,
        ),
    ] = False,
    env: Annotated[
        tuple[str, ...],
        Parameter(
            name="--env",
            group=ADVANCED_PARAMS,
            help=(
                "Environment variables for the spawned harness "
                "in KEY=VALUE form (repeatable). "
                "Overrides config-level [env] defaults."
            ),
            negative_iterable=(),
        ),
    ] = (),
) -> None:
    # Passthrough lives on GlobalOptions, not a function parameter — see
    # _split_passthrough_args() for why cyclopts can't handle ``--`` correctly.
    passthrough = _get_global_options().passthrough_args
    global_harness = _get_global_options().harness
    caller_cwd = Path.cwd().as_posix()
    resolved_continue_from = (continue_from or "").strip() or None
    raw_fork_from = (fork_from or "").strip() or None
    raw_fork_fresh_from = (fork_fresh_from or "").strip() or None

    # Resolve --yolo / --approval interaction.
    if yolo and approval is not None:
        raise ValueError(
            "Cannot use --yolo with --approval (--yolo is shorthand for --approval never)."
        )
    resolved_approval = approval if approval is not None else ("never" if yolo else None)
    parsed_skills = parse_csv_list(skills, field_name="skills")
    resolved_goal = normalize_goal(goal)
    agent_launch = resolve_agent_launch_input(agent)

    fork_resolution = validate_fork_mode(
        fork_from=raw_fork_from,
        fork_fresh_from=raw_fork_fresh_from,
        continue_from=resolved_continue_from,
        context_from=context_from,
        agent=agent,
        model=model,
        skills=skills,
    )
    shared_launch_kwargs = _shared_launch_input_kwargs(
        dry_run=dry_run,
        verbose=verbose,
        quiet=quiet,
        stream=stream,
        background=background,
        project_root=None,
        timeout=timeout,
        resident_rearm_budget=resident_rearm_budget,
        approval=resolved_approval,
        autocompact=autocompact,
        autocompact_pct=autocompact_pct,
        effort=effort,
        sandbox=sandbox,
        harness=global_harness,
        passthrough_args=passthrough,
        task_ping_interval=task_ping_interval,
        task_ping_reset_on_activity=task_ping_reset_on_activity,
        debug=debug,
        env=env,
    )
    logger.debug(
        "spawn_launcher_phase",
        phase="prompt_resolution",
        launcher_pid=os.getpid(),
    )
    _validate_positional_spawn_prompt(positional_prompt, subcommands=subcommands)
    if positional_prompt is not None and prompt is not None:
        raise ValueError("cannot specify both a positional prompt and -p/--prompt")
    resolved_prompt = _resolve_spawn_prompt(
        prompt if prompt is not None else positional_prompt,
        prompt_file,
        has_files=bool(references),
        is_continue=resolved_continue_from is not None,
    )
    logger.debug(
        "spawn_launcher_phase",
        phase="prompt_resolution_complete",
        launcher_pid=os.getpid(),
    )
    normalized_task_dir = (task_dir or "").strip() or None
    if resolved_continue_from is not None and normalized_task_dir is not None:
        raise ValueError("--continue does not accept --task-dir. Use --fork --task-dir to diverge.")

    fork_source_ref = fork_resolution.fork_ref or fork_resolution.fork_fresh_ref
    if fork_source_ref is not None:
        fork_is_fresh = fork_resolution.is_fresh
        result = spawn_fork_sync(
            SpawnForkInput(
                source_ref=fork_source_ref,
                prompt=resolved_prompt,
                model=model if fork_is_fresh else "",
                files=references,
                template_vars=template_vars,
                agent=agent_launch.agent if fork_is_fresh else None,
                agent_opt_out=agent_launch.agent_opt_out,
                skills=parsed_skills if fork_is_fresh else (),
                goal=resolved_goal,
                inherit_source_skills=True if not fork_is_fresh else skills is None,
                desc=desc,
                work=work,
                task_dir=normalized_task_dir,
                caller_cwd=caller_cwd,
                **shared_launch_kwargs,
            ),
            sink=_current_output_sink(),
            prepared=_prepare_spawn_runtime_write(),
        )
    elif resolved_continue_from is not None:
        result = spawn_continue_sync(
            SpawnContinueInput(
                spawn_id=resolved_continue_from,
                prompt=resolved_prompt,
                model=model,
                files=references,
                template_vars=template_vars,
                agent=agent_launch.agent,
                agent_opt_out=agent_launch.agent_opt_out,
                skills=parsed_skills,
                goal=resolved_goal,
                desc=desc,
                work=work,
                task_dir=normalized_task_dir,
                caller_cwd=caller_cwd,
                **shared_launch_kwargs,
            ),
            sink=_current_output_sink(),
            prepared=_prepare_spawn_runtime_write(),
        )
    else:
        result = spawn_create_sync(
            SpawnCreateInput(
                prompt=resolved_prompt,
                model=model,
                files=references,
                context_from=fork_resolution.resolved_context_from,
                template_vars=template_vars,
                agent=agent_launch.agent,
                agent_opt_out=agent_launch.agent_opt_out,
                skills=parsed_skills,
                desc=desc,
                goal=resolved_goal,
                work=work,
                task_dir=normalized_task_dir,
                caller_cwd=caller_cwd,
                **shared_launch_kwargs,
            ),
            sink=_current_output_sink(),
            prepared=_prepare_spawn_runtime_write(),
            on_spawn_id=_foreground_spawn_id_notifier(quiet=quiet),
        )
    output_format = _get_global_options().output.format
    if output_format != "json" and metadata:
        detailed_output = _spawn_create_metadata_output(result)
        if detailed_output is not None:
            emit(detailed_output.format_text(FormatContext(verbosity=1)))
        else:
            emit(result.format_text(FormatContext(verbosity=1)))
    elif output_format != "json" and verbose:
        emit(result.format_text(FormatContext(verbosity=1)))
    else:
        emit(result)
    exit_code = _spawn_create_exit_code(result)
    if exit_code != 0:
        raise SystemExit(exit_code)


def _spawn_list(
    emit: Any,
    status: Annotated[
        str | None,
        Parameter(
            name="--status",
            help=(
                "Filter by status: queued, running, finalizing, succeeded, "
                "failed, cancelled, timed_out."
            ),
        ),
    ] = None,
    view: Annotated[
        str,
        Parameter(
            name="--view",
            help=(
                "Preset list view: active, recent (recent active spawns), "
                "all, running, queued, completed, failed, cancelled, timed_out. "
                "Default: active."
            ),
        ),
    ] = "active",
    all_spawns: Annotated[
        bool,
        Parameter(name="--all", help="Shortcut for --view all."),
    ] = False,
    model: Annotated[
        str | None,
        Parameter(name="--model", help="Filter by model id."),
    ] = None,
    profile: Annotated[
        str | None,
        Parameter(name="--profile", help="Filter by agent profile name."),
    ] = None,
    primary: Annotated[
        bool,
        Parameter(name="--primary", help="Only show primary spawns."),
    ] = False,
    limit: Annotated[
        int, Parameter(name="--limit", help="Maximum number of spawns to return.")
    ] = 20,
    failed: Annotated[
        bool,
        Parameter(name="--failed", help="Shortcut for status=failed."),
    ] = False,
) -> None:
    normalized_status: SpawnStatus | None = None
    normalized_statuses: tuple[SpawnStatus, ...] | None = None
    normalized_view = view.strip().lower() if view.strip() else "active"
    if all_spawns:
        normalized_view = "all"
        if limit == 20:  # user didn't override the default
            limit = 5
    view_map: dict[str, tuple[SpawnStatus, ...]] = {
        "active": _ACTIVE_VIEW_STATUSES,
        "recent": _ACTIVE_VIEW_STATUSES,
        "all": (),
        "running": (SpawnStatus.RUNNING,),
        "queued": (SpawnStatus.QUEUED,),
        "completed": (SpawnStatus.SUCCEEDED,),
        "failed": (SpawnStatus.FAILED,),
        "cancelled": (SpawnStatus.CANCELLED,),
        "timed_out": (SpawnStatus.TIMED_OUT,),
    }
    if normalized_view not in view_map:
        supported = ", ".join(view_map)
        raise ValueError(f"Unsupported spawn view '{view}'. Supported views: {supported}")

    if status is not None and status.strip():
        candidate = status.strip()
        if candidate not in _SPAWN_STATUS_VALUES:
            raise ValueError(f"Unsupported spawn status '{status}'")
        normalized_status = SpawnStatus(candidate)
    elif failed:
        normalized_statuses = (SpawnStatus.FAILED,)
    else:
        mapped_statuses = view_map[normalized_view]
        normalized_statuses = mapped_statuses

    result = spawn_list_sync(
        SpawnListInput(
            status=normalized_status,
            statuses=normalized_statuses,
            model=model,
            profile=profile,
            primary=primary,
            limit=limit,
            failed=False,
        ),
        sink=_current_output_sink(),
        prepared=_prepare_spawn_runtime_read(),
    )
    emit(result)


def _spawn_children(
    emit: Any,
    spawn_id: Annotated[
        str,
        Parameter(name="spawn_id", help="Parent spawn ID."),
    ],
) -> None:
    emit(
        spawn_children_sync(
            SpawnChildrenInput(spawn_id=spawn_id),
            sink=_current_output_sink(),
            prepared=_prepare_spawn_runtime_read(),
        )
    )


def _spawn_show(
    emit: Any,
    spawn_ids: Annotated[
        tuple[str, ...],
        Parameter(name="spawn_id", help="One or more spawn IDs to show."),
    ],
    verbose: Annotated[
        bool,
        Parameter(name="--verbose", help="Include internal spawn diagnostics.", show=True),
    ] = False,
    report: Annotated[
        bool,
        Parameter(
            name="--report",
            help=(
                "Include full spawn report body in output (default: enabled). "
                "Use --no-report to omit."
            ),
        ),
    ] = True,
) -> None:
    _spawn_show_like(
        emit,
        spawn_ids=spawn_ids,
        verbose=verbose,
        build_input=lambda spawn_id: SpawnShowInput(
            spawn_id=spawn_id,
            include_report_body=report,
        ),
        handler=spawn_show_sync,
    )


def _spawn_status(
    emit: Any,
    spawn_ids: Annotated[
        tuple[str, ...],
        Parameter(name="spawn_id", help="One or more spawn IDs to show."),
    ],
    verbose: Annotated[
        bool,
        Parameter(name="--verbose", help="Include internal spawn diagnostics.", show=True),
    ] = False,
    report: Annotated[
        bool,
        Parameter(
            name="--report",
            help="Include report body in output (default: disabled).",
        ),
    ] = False,
) -> None:
    _spawn_show_like(
        emit,
        spawn_ids=spawn_ids,
        verbose=verbose,
        build_input=lambda spawn_id: SpawnStatusInput(
            spawn_id=spawn_id,
            include_report_body=report,
        ),
        handler=spawn_status_sync,
    )


def _spawn_show_like(
    emit: Any,
    *,
    spawn_ids: tuple[str, ...],
    verbose: bool,
    build_input: Callable[[str], SpawnShowInput | SpawnStatusInput],
    handler: Callable[..., Any],
) -> None:
    sink = _current_output_sink()
    prepared = _prepare_spawn_runtime_read()
    results = tuple(
        handler(
            build_input(spawn_id),
            sink=sink,
            prepared=prepared,
        )
        for spawn_id in spawn_ids
    )

    output_format = _get_global_options().output.format
    if len(results) == 1:
        if output_format != "json" and verbose:
            emit(results[0].format_text(FormatContext(verbosity=1)))
        else:
            emit(results[0])
        return

    if output_format == "json":
        emit(list(results))
        return

    fmt_ctx = FormatContext(verbosity=1) if verbose else None
    emit("\n\n".join(result.format_text(fmt_ctx) for result in results))


def _spawn_stats(
    emit: Any,
    spawn_id: Annotated[
        str | None,
        Parameter(name="spawn_id", help="Spawn ID to get stats for (includes descendants)."),
    ] = None,
    session: Annotated[
        str | None,
        Parameter(name="--session", help="Only include spawns for this session id."),
    ] = None,
    flat: Annotated[
        bool,
        Parameter(name="--flat", help="Only show the specified spawn, exclude descendants."),
    ] = False,
) -> None:
    emit(
        spawn_stats_sync(
            SpawnStatsInput(
                spawn_id=spawn_id,
                session=session,
                flat=flat,
            ),
            sink=_current_output_sink(),
            prepared=_prepare_spawn_runtime_read(),
        )
    )


def _spawn_cancel(
    emit: Any,
    spawn_id: str,
) -> None:
    result = spawn_cancel_sync(
        SpawnCancelInput(
            spawn_id=spawn_id,
        ),
        sink=_current_output_sink(),
        prepared=_prepare_spawn_runtime_write(),
    )
    emit(result)
    if result.status == "failed":
        raise SystemExit(1)


def _spawn_wait(
    emit: Any,
    spawn_ids: Annotated[
        tuple[str, ...],
        Parameter(
            name="spawn_id",
            help=(
                "Spawn IDs to wait for. Omit to discover your session's pending "
                "spawns and wait on them."
            ),
        ),
    ] = (),
    timeout: Annotated[
        float | None,
        Parameter(
            name="--timeout",
            help="Hard timeout in minutes. Overrides checkpoint behavior.",
        ),
    ] = None,
    yield_after_secs: Annotated[
        float | None,
        Parameter(
            name="--yield-after-secs",
            help=(
                "Seconds before `wait` yields cleanly so you keep the turn. Overrides the "
                "harness-aware default. Exits cleanly when reached."
            ),
        ),
    ] = None,
    fail_fast: Annotated[
        bool,
        Parameter(
            name="--fail-fast",
            help="Return as soon as any spawn fails, listing spawns still pending.",
        ),
    ] = False,
    verbose: Annotated[
        bool,
        Parameter(name="--verbose", help="Enable verbose wait status output.", show=True),
    ] = False,
    metadata: Annotated[
        bool,
        Parameter(
            name="--metadata",
            help=(
                "Include detailed spawn metadata in text output "
                "(report-first view remains primary)."
            ),
        ),
    ] = False,
    quiet: Annotated[
        bool,
        Parameter(name="--quiet", help="Suppress wait progress output.", show=True),
    ] = False,
    report: Annotated[
        bool,
        Parameter(
            name="--report",
            help="Include full report body in output (default: enabled). Use --no-report to omit.",
        ),
    ] = True,
) -> None:
    result = spawn_wait_sync(
        SpawnWaitInput(
            spawn_ids=spawn_ids,
            timeout=timeout,
            yield_after_secs=yield_after_secs,
            timeout_explicit=timeout is not None,
            fail_fast=fail_fast,
            verbose=verbose,
            quiet=quiet,
            include_report_body=report,
        ),
        sink=_current_output_sink(),
        prepared=_prepare_spawn_runtime_read(),
    )
    output_format = _get_global_options().output.format
    if output_format != "json" and (metadata or verbose):
        emit(result.format_text(FormatContext(verbosity=1)))
    else:
        emit(result)
    if result.checkpoint:
        return
    if result.any_failed:
        raise SystemExit(1)


def _spawn_create_metadata_output(result: SpawnActionOutput) -> Any | None:
    if result.spawn_id is None or result.background or result.status == "dry-run":
        return None
    try:
        return spawn_show_sync(
            SpawnShowInput(
                spawn_id=result.spawn_id,
                include_report_body=True,
            ),
            sink=_current_output_sink(),
            prepared=_prepare_spawn_runtime_read(),
        )
    except (KeyError, RuntimeError, ValueError, FileNotFoundError, OSError):
        return None


def _spawn_files(
    emit: Any,
    spawn_id: Annotated[
        str,
        Parameter(
            help=(
                "Spawn ID. Lists the files this spawn changed, one per line."
            ),
        ),
    ],
    null: Annotated[
        bool,
        Parameter(name=["-0", "--null"], help="Null-delimited output for xargs -0."),
    ] = False,
) -> None:
    result = spawn_files_sync(
        SpawnWrittenFilesInput(spawn_id=spawn_id),
        sink=_current_output_sink(),
        prepared=_prepare_spawn_runtime_read(),
    )
    if null and result.written_files:
        import sys

        sys.stdout.write("\0".join(result.written_files))
        sys.stdout.flush()
    else:
        emit(result)


def _spawn_subagents(emit: Any) -> None:
    emit(
        spawn_subagents_sync(
            SpawnSubagentsInput(),
            sink=_current_output_sink(),
            prepared=_prepare_spawn_runtime_read(),
        )
    )


def _spawn_cancel_all(
    emit: Any,
    work: Annotated[
        str | None,
        Parameter(name="--work", help="Only cancel running spawns for this work item."),
    ] = None,
    include_primaries: Annotated[
        bool,
        Parameter(name="--include-primaries", help="Also cancel primary sessions."),
    ] = False,
    include_others: Annotated[
        bool,
        Parameter(
            name="--include-others",
            help="Also cancel spawns from other sessions (or when no session context exists).",
        ),
    ] = False,
) -> None:
    result = spawn_cancel_all_sync(
        SpawnCancelAllInput(
            work=work,
            include_primaries=include_primaries,
            include_others=include_others,
        ),
        ctx=RuntimeContext.from_environment(),
        sink=_current_output_sink(),
        prepared=_prepare_spawn_runtime_write(),
    )
    emit(result)
    if result.failed_count:
        raise SystemExit(1)


def _spawn_done(
    emit: Any,
    spawn_id: Annotated[
        str | None,
        Parameter(help="Spawn ID to finish (defaults to MERIDIAN_SPAWN_ID)."),
    ] = None,
) -> None:
    result = spawn_done_sync(
        SpawnSignalInput(spawn_id=spawn_id),
        ctx=RuntimeContext.from_environment(),
        sink=_current_output_sink(),
        prepared=_prepare_spawn_runtime_write(),
    )
    emit(result)
    if result.status == "failed":
        raise SystemExit(1)


def _spawn_rearm(
    emit: Any,
    spawn_id: Annotated[
        str | None,
        Parameter(help="Spawn ID to keep resident (defaults to MERIDIAN_SPAWN_ID)."),
    ] = None,
) -> None:
    result = spawn_rearm_sync(
        SpawnSignalInput(spawn_id=spawn_id),
        ctx=RuntimeContext.from_environment(),
        sink=_current_output_sink(),
        prepared=_prepare_spawn_runtime_write(),
    )
    emit(result)
    if result.status == "failed":
        raise SystemExit(1)


def _spawn_inject(
    spawn_id: Annotated[
        str,
        Parameter(help="Spawn ID to inject into."),
    ],
    message: Annotated[
        str,
        Parameter(
            help=(
                "Real user direction to forward mid-run."
            ),
        ),
    ] = "",
) -> None:
    asyncio.run(
        inject_message(
            spawn_id,
            message,
        )
    )


def _spawn_log_removed(
    spawn_id: Annotated[
        str,
        Parameter(help="Spawn id."),
    ],
) -> None:
    raise ValueError(
        "`meridian spawn log` was removed. Use "
        f"`meridian session log {spawn_id}` for spawn progress or transcript output."
    )


def _append_spawn_help_epilogue(app: App) -> None:
    existing = app.help_epilogue or ""
    if existing and not existing.endswith("\n"):
        existing += "\n"
    app.help_epilogue = existing + "\n" + _SPAWN_USAGE_EPILOGUE


def register_spawn_commands(app: App, emit: Emitter) -> tuple[set[str], dict[str, str]]:
    """Register spawn CLI commands using registry metadata as source of truth."""

    _append_spawn_help_epilogue(app)

    handlers: dict[str, Callable[[], Callable[..., None]]] = {
        "meridian.spawn.subagents": lambda: partial(_spawn_subagents, emit),
        "meridian.spawn.children": lambda: partial(_spawn_children, emit),
        "meridian.spawn.files": lambda: partial(_spawn_files, emit),
        "meridian.spawn.list": lambda: partial(_spawn_list, emit),
        "meridian.spawn.stats": lambda: partial(_spawn_stats, emit),
        "meridian.spawn.show": lambda: partial(_spawn_show, emit),
        "meridian.spawn.status": lambda: partial(_spawn_status, emit),
        "meridian.spawn.cancel": lambda: partial(_spawn_cancel, emit),
        "meridian.spawn.cancelAll": lambda: partial(_spawn_cancel_all, emit),
        "meridian.spawn.wait": lambda: partial(_spawn_wait, emit),
    }
    registered, descriptions = register_extension_cli_group(
        app,
        registry=get_first_party_registry(),
        group="spawn",
        handlers=handlers,
        command_help_epilogues={
            "meridian.spawn.wait": _SPAWN_WAIT_HELP_EPILOGUE,
        },
        emit=emit,
    )
    app.command(
        partial(_spawn_done, emit),
        name="done",
        help="Signal a resident spawn to finish successfully.",
    )
    registered.add("spawn.done")
    descriptions["meridian.spawn.done"] = "Signal a resident spawn to finish successfully."
    app.command(
        partial(_spawn_rearm, emit),
        name="rearm",
        help="Signal a resident spawn to keep running.",
    )
    registered.add("spawn.rearm")
    descriptions["meridian.spawn.rearm"] = "Signal a resident spawn to keep running."
    app.command(
        _spawn_inject,
        name="inject",
        help=(
            "Forward real user direction to a running spawn; self-generated "
            "nudges waste its attention."
        ),
    )
    registered.add("spawn.inject")
    descriptions["meridian.spawn.inject"] = (
        "Forward real user direction to a running spawn; self-generated nudges waste its attention."
    )
    app.command(
        _spawn_log_removed,
        name="log",
        help="Removed. Use `meridian session log ID`.",
        show=False,
    )
    registered.add("spawn.log")
    descriptions["meridian.spawn.log"] = "Removed. Use `meridian session log ID`."
    subcommands = frozenset(command.removeprefix("spawn.") for command in registered)
    app.default(partial(_spawn_create, subcommands, emit))
    return registered, descriptions
