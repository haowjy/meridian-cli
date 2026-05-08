"""CLI command handlers for spawn.* operations."""

import asyncio
import sys
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import Annotated, Any, cast, get_args

from cyclopts import App, Parameter

from meridian.cli.ext_registration import register_extension_cli_group
from meridian.cli.spawn_inject import inject_message
from meridian.cli.utils import missing_fork_session_error, parse_csv_list
from meridian.lib.bootstrap.services import (
    RuntimeReadContext,
    RuntimeWriteContext,
    prepare_for_runtime_read,
    prepare_for_runtime_write,
)
from meridian.lib.config.project_root import resolve_project_root
from meridian.lib.core.domain import SpawnStatus
from meridian.lib.core.spawn_lifecycle import ACTIVE_SPAWN_STATUSES
from meridian.lib.extensions.registry import get_first_party_registry
from meridian.lib.launch.request import SessionRequest
from meridian.lib.ops.reference import resolve_session_reference
from meridian.lib.ops.runtime import resolve_runtime_root_for_read
from meridian.lib.ops.spawn.api import (
    SpawnActionOutput,
    SpawnCancelAllInput,
    SpawnCancelInput,
    SpawnContinueInput,
    SpawnCreateInput,
    SpawnListEntry,
    SpawnListInput,
    SpawnListOutput,
    SpawnShowInput,
    SpawnStatsInput,
    SpawnWaitInput,
    SpawnWrittenFilesInput,
    spawn_cancel_all_sync,
    spawn_cancel_sync,
    spawn_continue_sync,
    spawn_create_sync,
    spawn_files_sync,
    spawn_list_sync,
    spawn_show_sync,
    spawn_stats_sync,
    spawn_wait_sync,
)
from meridian.lib.ops.spawn.query import resolve_spawn_reference
from meridian.lib.state import spawn_store

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
    opts = _get_global_options()
    if opts.project_root is not None:
        return prepare_for_runtime_read(opts.project_root)
    return prepare_for_runtime_read(resolve_project_root())


def _prepare_spawn_runtime_write() -> RuntimeWriteContext:
    opts = _get_global_options()
    if opts.project_root is not None:
        return prepare_for_runtime_write(opts.project_root)
    return prepare_for_runtime_write(resolve_project_root())


def _spawn_create_exit_code(result: SpawnActionOutput) -> int:
    if result.exit_code is not None:
        return result.exit_code
    if result.status in {"succeeded", "running", "finalizing", "dry-run"}:
        return 0
    return 1


def _desc_or_prompt_summary(desc: str | None, prompt: str | None) -> str | None:
    normalized_desc = (desc or "").strip()
    if normalized_desc:
        return normalized_desc
    normalized_prompt = (prompt or "").strip()
    if not normalized_prompt:
        return None
    compact_prompt = " ".join(normalized_prompt.split()).strip()
    if len(compact_prompt) <= 50:
        return compact_prompt
    return f"{compact_prompt[:47].rstrip()}..."


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
    work: Annotated[
        str,
        Parameter(name="--work", help="Associate the spawn with a work item id."),
    ] = "",
    dry_run: Annotated[
        bool,
        Parameter(name="--dry-run", help="Preview without executing harness."),
    ] = False,
    verbose: Annotated[
        bool,
        Parameter(name="--verbose", help="Enable verbose spawn logging.", show=True),
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
            help="Autocompact threshold percentage (1-100). Overrides agent profile.",
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
                "Fork from a session ref: chat id (c123), spawn id (p123), "
                "or raw harness session id."
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

    # Resolve --yolo / --approval interaction.
    if yolo and approval is not None:
        raise ValueError(
            "Cannot use --yolo with --approval (--yolo is shorthand for --approval yolo)."
        )
    resolved_approval = approval if approval is not None else ("yolo" if yolo else None)
    parsed_skills = parse_csv_list(skills, field_name="skills")
    resolved_fork_from = (fork_from or "").strip() or None
    resolved_prompt = _resolve_spawn_prompt(
        prompt,
        prompt_file,
        has_files=bool(references),
        is_continue=resolved_continue_from is not None,
    )

    if resolved_fork_from is not None and resolved_continue_from is not None:
        raise ValueError("Cannot combine --fork with --continue.")

    if resolved_fork_from is not None:
        if context_from:
            raise ValueError("Cannot combine --fork with --from (MVP limitation).")

        prepared = _prepare_spawn_runtime_write()
        resolved_reference = resolve_session_reference(prepared.project_root, resolved_fork_from)
        if resolved_reference.missing_harness_session_id:
            raise ValueError(missing_fork_session_error(resolved_fork_from))

        requested_model = model.strip()
        requested_agent = (agent or "").strip() or None
        requested_work = work.strip()

        inherited_skills = (
            resolved_reference.source_skills
            if skills is None and requested_agent is None
            else parsed_skills
        )

        result = spawn_create_sync(
            SpawnCreateInput(
                prompt=resolved_prompt,
                model=requested_model or (resolved_reference.source_model or ""),
                files=references,
                template_vars=template_vars,
                agent=requested_agent or resolved_reference.source_agent,
                skills=inherited_skills,
                desc=desc,
                work=requested_work or (resolved_reference.source_work_id or ""),
                dry_run=dry_run,
                verbose=verbose,
                quiet=quiet,
                stream=stream,
                background=background,
                timeout=timeout,
                approval=resolved_approval,
                autocompact=autocompact,
                effort=effort,
                sandbox=sandbox,
                harness=global_harness
                or (resolved_reference.harness if not requested_model else None),
                passthrough_args=passthrough,
                debug=debug,
                session=SessionRequest(
                    requested_harness_session_id=resolved_reference.harness_session_id,
                    continue_harness=resolved_reference.harness,
                    continue_source_tracked=resolved_reference.tracked,
                    continue_source_ref=resolved_fork_from,
                    continue_fork=True,
                    forked_from_chat_id=resolved_reference.source_chat_id,
                    source_execution_cwd=resolved_reference.source_execution_cwd,
                    source_claude_config_dir=resolved_reference.source_claude_config_dir,
                ),
            ),
            sink=_current_output_sink(),
            prepared=prepared,
        )
    elif resolved_continue_from is not None:
        if context_from:
            raise ValueError("Cannot use --from with --continue")
        result = spawn_continue_sync(
            SpawnContinueInput(
                spawn_id=resolved_continue_from,
                prompt=resolved_prompt,
                model=model,
                harness=global_harness,
                agent=agent,
                skills=parsed_skills,
                dry_run=dry_run,
                timeout=timeout,
                background=background,
                passthrough_args=passthrough,
                approval=resolved_approval,
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
                context_from=context_from,
                template_vars=template_vars,
                agent=agent,
                skills=parsed_skills,
                desc=desc,
                work=work,
                dry_run=dry_run,
                verbose=verbose,
                quiet=quiet,
                stream=stream,
                background=background,
                timeout=timeout,
                approval=resolved_approval,
                autocompact=autocompact,
                effort=effort,
                sandbox=sandbox,
                harness=global_harness,
                passthrough_args=passthrough,
                debug=debug,
            ),
            sink=_current_output_sink(),
            prepared=_prepare_spawn_runtime_write(),
        )
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
                "Preset list view: active, all, running, queued, completed, failed, cancelled. "
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
    normalized_ref = spawn_id.strip()
    if not normalized_ref:
        raise ValueError("spawn_id is required")
    prepared = _prepare_spawn_runtime_read()
    normalized_spawn_id = resolve_spawn_reference(
        prepared.project_root,
        normalized_ref,
        runtime_root=prepared.runtime_root,
    )
    runtime_root = prepared.runtime_root or resolve_runtime_root_for_read(prepared.project_root)
    from meridian.lib.state.reaper import reconcile_spawns

    children = list(
        reversed(
            reconcile_spawns(
                prepared.project_root,
                runtime_root,
                spawn_store.list_spawns(
                    runtime_root,
                    filters={"parent_id": normalized_spawn_id},
                ),
            )
        )
    )
    entries = tuple(
        SpawnListEntry(
            spawn_id=row.id,
            status=row.status,
            model=row.model or "",
            agent=row.agent or None,
            desc=_desc_or_prompt_summary(row.desc, row.prompt),
            duration_secs=row.duration_secs,
            cost_usd=row.total_cost_usd,
        )
        for row in children
    )
    emit(SpawnListOutput(spawns=entries, text_view="children"))


def _spawn_show(
    emit: Any,
    spawn_ids: Annotated[
        tuple[str, ...],
        Parameter(name="spawn_id", help="One or more spawn IDs to show."),
    ],
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
    sink = _current_output_sink()
    results = tuple(
        spawn_show_sync(
            SpawnShowInput(
                spawn_id=spawn_id,
                include_report_body=report,
            ),
            sink=sink,
            prepared=_prepare_spawn_runtime_read(),
        )
        for spawn_id in spawn_ids
    )

    if len(results) == 1:
        emit(results[0])
        return

    if _get_global_options().output.format == "json":
        emit(list(results))
        return

    emit("\n\n".join(result.format_text() for result in results))


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
    prepared = _prepare_spawn_runtime_write()
    resolved_spawn_id = resolve_spawn_reference(
        prepared.project_root,
        spawn_id,
        runtime_root=prepared.runtime_root,
    )
    result = spawn_cancel_sync(
        SpawnCancelInput(
            spawn_id=resolved_spawn_id,
        ),
        sink=_current_output_sink(),
        prepared=prepared,
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
    quiet: Annotated[
        bool,
        Parameter(name="--quiet", help="Suppress wait progress output.", show=True),
    ] = False,
    report: Annotated[
        bool,
        Parameter(
            name="--report",
            help="Include full report body in output (default: omitted).",
        ),
    ] = False,
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
    emit(result)
    if result.checkpoint:
        return
    if result.any_failed:
        raise SystemExit(1)


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
) -> None:
    result = spawn_cancel_all_sync(
        SpawnCancelAllInput(work=work),
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
    descriptions["meridian.spawn.inject"] = (
        "Inject a message into a running streaming spawn."
    )
    app.command(
        _spawn_log_removed,
        name="log",
        help="Removed. Use `meridian session log ID`.",
        show=False,
    )
    return registered, descriptions
