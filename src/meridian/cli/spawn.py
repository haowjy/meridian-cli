"""CLI command handlers for spawn.* operations."""

import asyncio
import sys
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import Annotated, Any, cast, get_args

from cyclopts import App, Parameter

from meridian.cli.argv_normalization import validate_fork_mode
from meridian.cli.ext_registration import register_extension_cli_group
from meridian.cli.spawn_inject import inject_message
from meridian.cli.utils import parse_csv_list, require_established_project_root
from meridian.lib.bootstrap.services import (
    RuntimeReadContext,
    RuntimeWriteContext,
    prepare_for_runtime_read,
    prepare_for_runtime_write,
)
from meridian.lib.core.context import RuntimeContext
from meridian.lib.core.domain import SpawnStatus
from meridian.lib.core.spawn_lifecycle import ACTIVE_SPAWN_STATUSES
from meridian.lib.core.util import FormatContext
from meridian.lib.extensions.registry import get_first_party_registry
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
    SpawnStatsInput,
    SpawnStatusInput,
    SpawnWaitInput,
    SpawnWrittenFilesInput,
    spawn_cancel_all_sync,
    spawn_cancel_sync,
    spawn_children_sync,
    spawn_continue_sync,
    spawn_create_sync,
    spawn_files_sync,
    spawn_fork_sync,
    spawn_list_sync,
    spawn_show_sync,
    spawn_stats_sync,
    spawn_status_sync,
    spawn_wait_sync,
)
from meridian.lib.ops.spawn.models import SpawnLaunchOptionUpdates, normalize_goal

Emitter = Callable[[Any], None]
_SPAWN_STATUS_VALUES: tuple[SpawnStatus, ...] = cast(
    "tuple[SpawnStatus, ...]", get_args(SpawnStatus)
)
_ACTIVE_VIEW_STATUSES: tuple[SpawnStatus, ...] = tuple(
    status for status in _SPAWN_STATUS_VALUES if status in ACTIVE_SPAWN_STATUSES
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
    return prepare_for_runtime_write(require_established_project_root())


def _spawn_create_exit_code(result: SpawnActionOutput) -> int:
    if result.exit_code is not None:
        return result.exit_code
    if result.status in {"succeeded", "running", "finalizing", "dry-run"}:
        return 0
    return 1


def _read_prompt_from_stdin(*, explicit_prompt_file_stdin: bool, allow_empty: bool = False) -> str:
    if sys.stdin.isatty():
        if explicit_prompt_file_stdin:
            raise ValueError("--prompt-file - requires stdin to be piped or redirected")
        raise ValueError("prompt required: pass --prompt-file, -p, or pipe stdin")
    try:
        prompt_text = sys.stdin.read()
    except UnicodeDecodeError as exc:
        raise ValueError("prompt stdin is not valid UTF-8") from exc
    if not prompt_text and not allow_empty:
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
            return _read_prompt_from_stdin(explicit_prompt_file_stdin=True)
        return _read_prompt_from_file(prompt_file)
    if not sys.stdin.isatty():
        prompt_text = _read_prompt_from_stdin(explicit_prompt_file_stdin=False, allow_empty=True)
        if prompt_text:
            return prompt_text
        if has_files or is_continue:
            return ""
        raise ValueError("prompt stdin is empty")
    if has_files or is_continue:
        return ""
    raise ValueError("prompt required: pass --prompt-file, -p, or pipe stdin")


def _shared_launch_input_kwargs(
    *,
    dry_run: bool,
    verbose: bool,
    quiet: bool,
    stream: bool,
    background: bool,
    project_root: str | None,
    timeout: float | None,
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
) -> SpawnLaunchOptionUpdates:
    return {
        "dry_run": dry_run,
        "verbose": verbose,
        "quiet": quiet,
        "stream": stream,
        "background": background,
        "project_root": project_root,
        "timeout": timeout,
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
    }


def _spawn_create(
    emit: Any,
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
            help=(
                "Read prompt from a file. Preferred for delegation. Use '-' to read stdin. "
                "If neither --prompt nor --prompt-file is set and stdin is piped, "
                "stdin is used as the prompt."
            ),
            allow_leading_hyphen=True,
        ),
    ] = None,
    template_vars: Annotated[
        tuple[str, ...],
        Parameter(
            name="--prompt-var",
            help=(
                "Prompt template variables in KEY=VALUE form (repeatable). "
                "Replaces {{KEY}} during prompt assembly."
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
            help="Reference files to include in prompt context (repeatable).",
            negative_iterable=(),
        ),
    ] = (),
    context_from: Annotated[
        tuple[str, ...],
        Parameter(
            name=["--from"],
            help=(
                "Inherit context from a prior spawn or chat/session.\n"
                "Spawn refs include the spawn report and files touched;\n"
                "chat refs point at the session log and primary spawn.\n"
                "Repeatable. Use when the new spawn needs prior\n"
                "conversation context."
            ),
            negative_iterable=(),
        ),
    ] = (),
    agent: Annotated[
        str | None,
        Parameter(name=["--agent", "-a"], help="Agent profile name to execute."),
    ] = None,
    skills: Annotated[
        str | None,
        Parameter(
            name=["--skills", "-s"],
            help="Comma-separated ad-hoc skills to load. Merged with agent profile skills.",
        ),
    ] = None,
    desc: Annotated[
        str,
        Parameter(name=["--desc", "--description"], help="Short description for the spawn."),
    ] = "",
    goal: Annotated[
        str | None,
        Parameter(
            name="--goal",
            help=(
                "Completion goal for this spawn. Injected as a bounded "
                "completion contract and persisted as spawn metadata."
            ),
        ),
    ] = None,
    work: Annotated[
        str,
        Parameter(name="--work", help="Associate the spawn with a work item id."),
    ] = "",
    task_dir: Annotated[
        str | None,
        Parameter(
            name="--task-dir",
            help=(
                "Override the source-code edit directory for this spawn only. "
                "Does not modify the work item's task_dir setting. "
                "Relative -f paths resolve against this directory."
            ),
        ),
    ] = None,
    dry_run: Annotated[
        bool,
        Parameter(name="--dry-run", help="Preview without executing harness."),
    ] = False,
    verbose: Annotated[
        bool,
        Parameter(name="--verbose", help="Enable verbose spawn logging.", show=True),
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
        Parameter(name="--quiet", help="Reduce non-essential command output.", show=True),
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
            help="Run in background and return immediately with spawn ID.",
        ),
    ] = False,
    timeout: Annotated[
        float | None,
        Parameter(
            name="--timeout",
            help="Maximum runtime in minutes before spawn timeout.",
        ),
    ] = None,
    approval: Annotated[
        str | None,
        Parameter(
            name="--approval",
            help="Approval mode: default, confirm, auto, yolo. Overrides agent profile.",
        ),
    ] = None,
    autocompact: Annotated[
        int | None,
        Parameter(
            name="--autocompact",
            help="Autocompact token threshold (minimum 1000). Overrides agent profile.",
        ),
    ] = None,
    autocompact_pct: Annotated[
        int | None,
        Parameter(
            name="--autocompact-pct",
            help="Percentage of context window for autocompact (1-100). Overrides agent profile.",
        ),
    ] = None,
    effort: Annotated[
        str | None,
        Parameter(
            name="--effort",
            help="Effort level: low, medium, high, xhigh. Overrides agent profile.",
        ),
    ] = None,
    sandbox: Annotated[
        str | None,
        Parameter(
            name="--sandbox",
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
            help=(
                "Fork from a session ref while preserving launch identity "
                "(agent/model/skills): chat id (c123), spawn id (p123), "
                "or raw harness session id."
            ),
        ),
    ] = None,
    fork_fresh_from: Annotated[
        str | None,
        Parameter(
            name="--fork-fresh",
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
) -> None:
    # Passthrough lives on GlobalOptions, not a function parameter — see
    # _split_passthrough_args() for why cyclopts can't handle ``--`` correctly.
    passthrough = _get_global_options().passthrough_args
    global_harness = _get_global_options().harness
    resolved_continue_from = (continue_from or "").strip() or None
    raw_fork_from = (fork_from or "").strip() or None
    raw_fork_fresh_from = (fork_fresh_from or "").strip() or None

    # Resolve --yolo / --approval interaction.
    if yolo and approval is not None:
        raise ValueError(
            "Cannot use --yolo with --approval (--yolo is shorthand for --approval yolo)."
        )
    resolved_approval = approval if approval is not None else ("yolo" if yolo else None)
    parsed_skills = parse_csv_list(skills, field_name="skills")
    resolved_goal = normalize_goal(goal)

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
    )
    resolved_prompt = _resolve_spawn_prompt(
        prompt,
        prompt_file,
        has_files=bool(references),
        is_continue=resolved_continue_from is not None,
    )
    normalized_task_dir = (task_dir or "").strip() or None
    if resolved_continue_from is not None and normalized_task_dir is not None:
        raise ValueError(
            "--continue does not accept --task-dir. Use --fork --task-dir to diverge."
        )

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
                agent=agent if fork_is_fresh else None,
                skills=parsed_skills if fork_is_fresh else (),
                goal=resolved_goal,
                inherit_source_skills=True if not fork_is_fresh else skills is None,
                desc=desc,
                work=work,
                task_dir=normalized_task_dir,
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
                agent=agent,
                skills=parsed_skills,
                goal=resolved_goal,
                desc=desc,
                work=work,
                task_dir=normalized_task_dir,
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
                agent=agent,
                skills=parsed_skills,
                desc=desc,
                goal=resolved_goal,
                work=work,
                task_dir=normalized_task_dir,
                **shared_launch_kwargs,
            ),
            sink=_current_output_sink(),
            prepared=_prepare_spawn_runtime_write(),
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
            help="Filter by status: queued, running, finalizing, succeeded, failed, cancelled.",
        ),
    ] = None,
    view: Annotated[
        str,
        Parameter(
            name="--view",
            help=(
                "Preset list view: active, recent (recent active spawns), "
                "all, running, queued, completed, failed, cancelled. "
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
        "running": ("running",),
        "queued": ("queued",),
        "completed": ("succeeded",),
        "failed": ("failed",),
        "cancelled": ("cancelled",),
    }
    if normalized_view not in view_map:
        supported = ", ".join(view_map)
        raise ValueError(f"Unsupported spawn view '{view}'. Supported views: {supported}")

    if status is not None and status.strip():
        candidate = status.strip()
        if candidate not in _SPAWN_STATUS_VALUES:
            raise ValueError(f"Unsupported spawn status '{status}'")
        normalized_status = candidate
    elif failed:
        normalized_statuses = ("failed",)
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
        Parameter(name="spawn_id", help="Spawn IDs to wait for (omit to wait for all pending)."),
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
                "Cache-preserving yield interval in seconds. Overrides harness-aware "
                "default. Exits cleanly when reached."
            ),
        ),
    ] = None,
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
    spawn_id: str,
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


def _spawn_inject(
    spawn_id: Annotated[
        str,
        Parameter(help="Spawn ID to inject into."),
    ],
    message: Annotated[
        str,
        Parameter(help="Message text to inject."),
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


def register_spawn_commands(app: App, emit: Emitter) -> tuple[set[str], dict[str, str]]:
    """Register spawn CLI commands using registry metadata as source of truth."""

    handlers: dict[str, Callable[[], Callable[..., None]]] = {
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
        emit=emit,
        default_handler=partial(_spawn_create, emit),
    )
    app.command(
        _spawn_inject,
        name="inject",
        help="Inject a message into a running streaming spawn.",
    )
    registered.add("spawn.inject")
    descriptions["meridian.spawn.inject"] = "Inject a message into a running streaming spawn."
    app.command(
        _spawn_log_removed,
        name="log",
        help="Removed. Use `meridian session log ID`.",
        show=False,
    )
    return registered, descriptions
