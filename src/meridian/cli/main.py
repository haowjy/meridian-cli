"""Cyclopts CLI entry point for meridian."""

import os
import subprocess
import sys
from collections.abc import Callable, Generator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, cast

from cyclopts import App, Parameter
from pydantic import BaseModel, ConfigDict

from meridian.cli.app_tree import (
    AGENT_ROOT_HELP as _AGENT_ROOT_HELP,
)
from meridian.cli.app_tree import (
    app,
    completion_app,
    config_app,
    hooks_app,
    models_app,
    report_app,
    session_app,
    spawn_app,
    streaming_app,
    telemetry_app,
    test_app,
    work_app,
    workspace_app,
)
from meridian.cli.argv_normalization import normalize_optional_value_flags
from meridian.cli.bootstrap import (
    extract_global_options as _bootstrap_extract_global_options,
)
from meridian.cli.bootstrap import (
    first_positional_token as _bootstrap_first_positional_token,
)
from meridian.cli.bootstrap import (
    is_root_help_request as _bootstrap_is_root_help_request,
)
from meridian.cli.bootstrap import (
    maybe_bootstrap_runtime_state,
    meridian_managed_env_default,
    temporary_config_env,
)
from meridian.cli.bootstrap import (
    split_passthrough_args as _bootstrap_split_passthrough_args,
)
from meridian.cli.bootstrap import (
    validate_top_level_command as _bootstrap_validate_top_level_command,
)
from meridian.cli.hooks_authority import (
    manual_hook_authority_scope,
    should_suppress_manual_hook_authority,
)
from meridian.cli.output import (
    CLIOutputProtocol,
    OutputConfig,
    OutputFormat,
    create_sink,
    flush_sink,
    normalize_output_format,
)
from meridian.cli.output import emit as emit_output
from meridian.cli.startup.catalog import COMMAND_CATALOG
from meridian.cli.startup.classify import classify_invocation
from meridian.cli.startup.policy import StartupClass, StateRequirement
from meridian.cli.startup.policy import TelemetryMode as StartupTelemetryMode
from meridian.cli.utils import parse_csv_list
from meridian.lib.core.depth import is_managed_meridian_session
from meridian.lib.core.sink import OutputSink
from meridian.lib.core.util import FormatContext
from meridian.lib.telemetry import emit_telemetry

if TYPE_CHECKING:
    from meridian.cli.mars_passthrough import MarsPassthroughRequest, MarsPassthroughResult


class GlobalOptions(BaseModel):
    """Top-level options that apply to all commands."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    output: OutputConfig
    config_file: str | None = None
    harness: str | None = None
    yes: bool = False
    no_input: bool = False
    # Future cleanup: `output_explicit` may be removable now that
    # `explicit_format` carries the resolved explicit output selection.
    output_explicit: bool = False
    force_agent: bool = False
    force_human: bool = False
    passthrough_args: tuple[str, ...] = ()
    sink: OutputSink | None = None
    explicit_format: OutputFormat | None = None
    project_root: Path | None = None
    directory_explicit: bool = False


_GLOBAL_OPTIONS: ContextVar[GlobalOptions | None] = ContextVar("_GLOBAL_OPTIONS", default=None)
_registered_command_groups: set[str] = set()
_group_commands_registered = False


def get_global_options() -> GlobalOptions:
    """Return parsed global options for current command."""

    default = GlobalOptions(output=OutputConfig(format="text"))
    return _GLOBAL_OPTIONS.get() or default


def _resolve_sink(opts: GlobalOptions | None) -> tuple[OutputSink, bool]:
    if opts is not None and opts.sink is not None:
        return opts.sink, False
    if opts is None:
        return create_sink(OutputConfig(format="text")), True
    return create_sink(opts.output), True


def current_output_sink() -> OutputSink:
    sink, _ = _resolve_sink(_GLOBAL_OPTIONS.get())
    return sink


def emit(payload: object, *, format_ctx: FormatContext | None = None) -> None:
    """Write command output using current output format settings."""

    options = get_global_options()
    sink, flush_after = _resolve_sink(options)
    if isinstance(payload, CLIOutputProtocol):
        emit_output(
            payload.to_cli_output(
                format=options.output.format,
                explicit_format=options.explicit_format,
                agent_mode=agent_mode_enabled(),
            ),
            sink=sink,
            format_ctx=format_ctx,
        )
    else:
        emit_output(payload, sink=sink, format_ctx=format_ctx)
    if flush_after:
        flush_sink(sink)


def _extract_global_options(argv: Sequence[str]) -> tuple[list[str], GlobalOptions]:
    cleaned, parsed = _bootstrap_extract_global_options(
        argv,
        normalize_output_format=lambda requested, json_mode: normalize_output_format(
            requested=requested,
            json_mode=json_mode,
        ),
    )
    explicit_format: OutputFormat | None = None
    if parsed.output_explicit:
        explicit_format = cast("OutputFormat", parsed.output_format)

    project_root: Path | None = None
    if parsed.directory is not None:
        project_root = Path(parsed.directory).expanduser().resolve()

    return cleaned, GlobalOptions(
        output=OutputConfig(format=cast("OutputFormat", parsed.output_format)),
        config_file=parsed.config_file,
        harness=parsed.harness,
        yes=parsed.yes,
        no_input=parsed.no_input,
        output_explicit=parsed.output_explicit,
        force_agent=parsed.force_agent,
        force_human=parsed.force_human,
        explicit_format=explicit_format,
        project_root=project_root,
        directory_explicit=parsed.directory_explicit,
    )


def _split_passthrough_args(argv: Sequence[str]) -> tuple[list[str], tuple[str, ...]]:
    """Split args at ``--`` so cyclopts never sees passthrough tokens.

    Workaround for a cyclopts bug: tokens after ``--`` are supposed to be
    positional-only, but cyclopts still assigns them to unfilled named
    parameters (e.g. ``--prompt`` absorbs ``--add-dir``).  We strip ``--``
    and everything after it before cyclopts parses, stash the passthrough
    tokens on ``GlobalOptions.passthrough_args``, and have handlers read
    them from there instead of from a ``*passthrough`` function parameter.
    """

    return _bootstrap_split_passthrough_args(argv)


def agent_mode_enabled() -> bool:
    return is_managed_meridian_session()


def _spawn_background_requested(argv: Sequence[str]) -> bool:
    if not argv or argv[0] != "spawn":
        return False
    for token in argv[1:]:
        if token == "--":
            break
        if token in {"--background", "--bg"}:
            return True
        if token.startswith("--background="):
            return True
    return False


def _resolve_output_format_for_command(
    *,
    argv: Sequence[str],
    explicit_format: OutputFormat | None,
    agent_mode: bool,
) -> OutputFormat:
    """Resolve effective output format based on command and context."""
    from typing import Literal

    from meridian.cli.output import resolve_effective_format

    agent_default_format: Literal["text", "json"] | None = None
    descriptor = classify_invocation(argv, COMMAND_CATALOG)
    if descriptor is not None and descriptor.default_output_mode in {"text", "json"}:
        agent_default_format = cast("Literal['text', 'json']", descriptor.default_output_mode)
        if (
            agent_mode
            and explicit_format is None
            and descriptor.command_path in {("spawn",), ("spawn", "create")}
            and _spawn_background_requested(argv)
        ):
            # Keep background submission wire output stable for agent-mode callers.
            agent_default_format = "json"

    return resolve_effective_format(
        explicit_format=explicit_format,
        agent_mode=agent_mode,
        agent_default_format=agent_default_format,
    )


def _interactive_terminal_attached() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _read_primary_prompt_from_stdin(*, explicit_prompt_file_stdin: bool) -> str:
    if sys.stdin.isatty():
        if explicit_prompt_file_stdin:
            raise ValueError("--prompt-file - requires stdin to be piped or redirected")
        raise ValueError("prompt stdin requires stdin to be piped or redirected")
    try:
        prompt_text = sys.stdin.read()
    except UnicodeDecodeError as exc:
        raise ValueError("prompt stdin is not valid UTF-8") from exc
    if not prompt_text:
        raise ValueError("prompt stdin is empty")
    return prompt_text


def _read_primary_prompt_from_file(prompt_file: str) -> str:
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


def _resolve_primary_prompt(prompt: str | None, prompt_file: str | None) -> str | None:
    if prompt is not None and prompt_file is not None:
        raise ValueError("cannot specify both -p and --prompt-file")
    if prompt is not None:
        return prompt
    if prompt_file is not None:
        if prompt_file == "-":
            return _read_primary_prompt_from_stdin(explicit_prompt_file_stdin=True)
        return _read_primary_prompt_from_file(prompt_file)
    return None


@app.default
def root(
    json_mode: Annotated[
        bool,
        Parameter(name="--json", help="Emit command output as JSON.", show=False),
    ] = False,
    output_format: Annotated[
        str | None,
        Parameter(name="--format", help="Set output format: text or json."),
    ] = None,
    config_file: Annotated[
        str | None,
        Parameter(name="--config", help="Path to a user config TOML overlay."),
    ] = None,
    directory: Annotated[
        str | None,
        Parameter(
            name=["-C", "--directory"],
            help="Resolve project root from this path instead of CWD.",
        ),
    ] = None,
    yes: Annotated[
        bool,
        Parameter(name="--yes", help="Auto-approve prompts when supported.", show=False),
    ] = False,
    no_input: Annotated[
        bool,
        Parameter(
            name="--no-input",
            help="Disable interactive prompts and fail if input is needed.",
            show=False,
        ),
    ] = False,
    force_agent: Annotated[
        bool,
        Parameter(name="--agent", help="Force agent mode for this invocation.", show=False),
    ] = False,
    continue_ref: Annotated[
        str | None,
        Parameter(
            name="--continue",
            help=(
                "Continue from a session ref: chat id (c123), spawn id (p123), "
                "or raw harness session id."
            ),
        ),
    ] = None,
    fork_ref: Annotated[
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
    fork_fresh_ref: Annotated[
        str | None,
        Parameter(
            name="--fork-fresh",
            help=(
                "Fork from a session ref and allow launch identity changes "
                "(agent/model/skills). This may reduce prompt-cache locality."
            ),
        ),
    ] = None,
    from_ref: Annotated[
        str | None,
        Parameter(
            name="--from",
            help=(
                "Start a fresh primary session with context from a prior spawn or "
                "session ref. Does not fork transcript lineage."
            ),
        ),
    ] = None,
    references: Annotated[
        tuple[str, ...],
        Parameter(
            name=["--file", "-f"],
            help="Reference files to include in primary prompt context (repeatable).",
            negative_iterable=(),
        ),
    ] = (),
    prompt_file: Annotated[
        str | None,
        Parameter(
            name="--prompt-file",
            help="Read primary prompt text from a file. Use '-' to read stdin.",
            allow_leading_hyphen=True,
        ),
    ] = None,
    prompt: Annotated[
        str | None,
        Parameter(
            name=["-p", "--prompt"],
            help="Inline literal primary prompt text.",
        ),
    ] = None,
    skills: Annotated[
        tuple[str, ...],
        Parameter(
            name="--skills",
            help="Comma-separated skill overrides for the primary agent. Repeatable.",
            negative_iterable=(),
        ),
    ] = (),
    goal: Annotated[
        str | None,
        Parameter(
            name="--goal",
            help="Completion goal injected as a bounded completion contract.",
        ),
    ] = None,
    model: Annotated[
        str,
        Parameter(name=["--model", "-m"], help="Model id or alias for primary harness."),
    ] = "",
    harness: Annotated[
        str | None,
        Parameter(
            name="--harness",
            help="Force harness id (claude, codex, cursor, opencode, or pi).",
        ),
    ] = None,
    agent: Annotated[
        str | None,
        Parameter(
            name=["--agent", "-a"],
            help='Agent profile name for the primary agent. Use -a "" for no profile.',
        ),
    ] = None,
    work: Annotated[
        str,
        Parameter(name="--work", help="Attach the primary session to a work item id."),
    ] = "",
    task_dir: Annotated[
        str | None,
        Parameter(
            name="--task-dir",
            help=(
                "Override the source-code edit directory for this primary launch only. "
                "Does not modify the work item's task_dir setting. "
                "Relative -f paths resolve against this directory."
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
        Parameter(name="--effort", help="Effort level: low, medium, high, xhigh, max."),
    ] = None,
    sandbox: Annotated[
        str | None,
        Parameter(
            name="--sandbox",
            help=("Sandbox mode: default, read-only, workspace-write, danger-full-access."),
        ),
    ] = None,
    approval: Annotated[
        str | None,
        Parameter(
            name="--approval",
            help="Approval mode: default, confirm, auto, never. Overrides agent profile.",
        ),
    ] = None,
    timeout: Annotated[
        float | None,
        Parameter(name="--timeout", help="Maximum runtime in minutes."),
    ] = None,
    dry_run: Annotated[
        bool,
        Parameter(name="--dry-run", help="Preview launch command without starting harness."),
    ] = False,
) -> None:
    """Launch or resume the primary harness."""

    if yolo and approval is not None:
        raise ValueError("Cannot combine --yolo with --approval.")

    resolved_prompt = _resolve_primary_prompt(prompt, prompt_file)
    parsed_skills: list[str] = []
    for token in skills:
        parsed_skills.extend(parse_csv_list(token, field_name="skills"))

    if _GLOBAL_OPTIONS.get() is None:
        resolved = normalize_output_format(requested=output_format, json_mode=json_mode)
        _GLOBAL_OPTIONS.set(
            GlobalOptions(
                output=OutputConfig(format=resolved),
                config_file=config_file,
                harness=harness,
                yes=yes,
                no_input=no_input,
                force_agent=force_agent,
            )
        )

    global_harness = get_global_options().harness
    explicit_harness = harness.strip() if harness is not None and harness.strip() else None
    if global_harness and explicit_harness and global_harness != explicit_harness:
        raise ValueError(
            f"Conflicting harness selections: '{global_harness}' and '{explicit_harness}'."
        )

    normalized_task_dir = (task_dir or "").strip() or None

    from meridian.cli import primary_launch

    emit(
        primary_launch.run_primary_launch(
            project_root=get_global_options().project_root,
            continue_ref=continue_ref,
            fork_ref=fork_ref,
            fork_fresh_ref=fork_fresh_ref,
            from_ref=from_ref,
            model=model,
            harness=global_harness or explicit_harness,
            agent=agent,
            work=work,
            task_dir=normalized_task_dir,
            yolo=yolo,
            approval=approval,
            autocompact=autocompact,
            autocompact_pct=autocompact_pct,
            effort=effort,
            sandbox=sandbox,
            timeout=timeout,
            dry_run=dry_run,
            passthrough=get_global_options().passthrough_args,
            reference_files=tuple(references),
            prompt=resolved_prompt,
            skills=tuple(parsed_skills),
            goal=goal,
        )
    )


@app.command(name="serve")
def serve() -> None:
    """Start FastMCP server on stdio."""
    from meridian.server.main import run_server

    run_server()


def _execute_mars_passthrough(
    request: "MarsPassthroughRequest",
) -> "MarsPassthroughResult":
    from meridian.cli import mars_passthrough

    return mars_passthrough.execute_mars_passthrough(request, run=subprocess.run, stderr=sys.stderr)


def _run_mars_passthrough(
    args: Sequence[str],
    *,
    output_format: str | None = None,
) -> None:
    from meridian.cli import mars_passthrough

    return mars_passthrough.run_mars_passthrough(
        args,
        output_format=output_format,
        resolve_executable=mars_passthrough.resolve_mars_executable,
        parse_request=mars_passthrough.parse_mars_passthrough,
        execute_request=_execute_mars_passthrough,
        stdout=sys.stdout,
        stderr=sys.stderr,
    )


@app.command(name="mars")
def mars(
    *args: Annotated[
        str,
        Parameter(
            help="Arguments forwarded to mars.",
            show=False,
        ),
    ],
) -> None:
    """Forward all arguments to the bundled mars CLI."""

    _run_mars_passthrough(args, output_format=get_global_options().output.format)


@app.command(name="init")
def init_alias(
    path: Annotated[
        str | None,
        Parameter(name="path", help="Optional project path to initialize."),
    ] = None,
    link: Annotated[
        list[str] | None,
        Parameter(
            name="--link",
            help="Link .mars/ into tool directory after config bootstrap (for example .claude).",
        ),
    ] = None,
    add: Annotated[
        list[str] | None,
        Parameter(
            name="--add",
            help="Package specifier(s) to install (e.g. owner/repo). Repeatable.",
        ),
    ] = None,
) -> None:
    """Initialize meridian in the current project or provided path."""

    from meridian.cli import mars_passthrough
    from meridian.lib.ops.config import ConfigInitInput, config_init_sync

    project_root = mars_passthrough.resolve_init_project_root(path)

    if add or link:
        from meridian.lib.ops.init_ops import run_init_flow

        result = run_init_flow(
            project_root=project_root,
            add_sources=add or [],
            link_targets=link,
            output_format=get_global_options().output.format,
        )
        emit(result)
        return

    # Truly bare init (no --add, no --link): bootstrap config only
    emit(config_init_sync(ConfigInitInput(project_root=project_root.as_posix())))


def _first_command_token(argv: Sequence[str]) -> str | None:
    for arg in argv:
        if not arg.startswith("-"):
            return arg
    return None


def _is_help_request(argv: Sequence[str]) -> bool:
    return any(token in {"--help", "-h"} for token in argv)


def _command_app_from_root(group_name: str) -> App | None:
    commands_obj = object.__getattribute__(app, "_commands")
    if not isinstance(commands_obj, dict):
        return None
    commands = cast("dict[str, object]", commands_obj)
    command = commands.get(group_name)
    if isinstance(command, App):
        return command
    return None


def _agent_help_group_apps() -> dict[str, App]:
    from meridian.cli.agent_help import AGENT_HELP_SUPPLEMENTS

    static_group_apps: dict[str, App] = {
        "spawn": spawn_app,
        "session": session_app,
        "work": work_app,
        "config": config_app,
    }
    group_apps: dict[str, App] = {}
    for group_name in AGENT_HELP_SUPPLEMENTS:
        group_app = static_group_apps.get(group_name)
        if group_app is None:
            group_app = _command_app_from_root(group_name)
        if group_app is not None:
            group_apps[group_name] = group_app
    return group_apps


def _apply_curated_help_for_registered_groups(*, agent_mode: bool) -> None:
    from meridian.cli.agent_help import apply_agent_help

    for group_name, group_app in _agent_help_group_apps().items():
        if group_name in _registered_command_groups:
            apply_agent_help(group_app, group_name, agent_mode=agent_mode)


def _register_commands_for_invocation(
    argv: Sequence[str],
    *,
    agent_mode: bool = False,
) -> None:
    """Register only the command group needed for the current invocation."""

    global _group_commands_registered

    if not _group_commands_registered:
        first_token = _first_command_token(argv)

        def _register_once(name: str, register: Callable[[], None]) -> None:
            if name in _registered_command_groups:
                return
            register()
            _registered_command_groups.add(name)

        def _register_spawn() -> None:
            from meridian.cli.spawn import register_spawn_commands

            register_spawn_commands(spawn_app, emit)

        def _register_session() -> None:
            from meridian.cli.session_cmd import register_session_commands

            register_session_commands(session_app, emit)

        def _register_work() -> None:
            from meridian.cli.work_cmd import register_work_commands

            register_work_commands(work_app, emit)

        def _register_config() -> None:
            from meridian.cli.config_cmd import register_config_commands

            register_config_commands(config_app, emit)

        def _register_hooks() -> None:
            from meridian.cli.hooks_commands import register_hooks_commands

            register_hooks_commands(hooks_app, emit)

        def _register_models() -> None:
            from meridian.cli.models_cmd import register_models_commands

            register_models_commands(models_app, emit)

        def _register_ext() -> None:
            from meridian.cli.ext_cmd import register_ext_commands

            register_ext_commands(
                app,
                emit=emit,
                resolve_global_format=lambda: get_global_options().output.format,
            )

        def _register_telemetry() -> None:
            from meridian.cli.telemetry_cmd import register_telemetry_commands

            register_telemetry_commands(telemetry_app, emit)

        def _register_workspace() -> None:
            from meridian.cli.workspace_cmd import register_workspace_commands

            register_workspace_commands(workspace_app, emit)

        def _register_doctor() -> None:
            from meridian.cli.doctor_cmd import register_doctor_command

            register_doctor_command(app, emit)

        def _register_bootstrap() -> None:
            from meridian.cli.bootstrap_cmd import register_bootstrap_command

            register_bootstrap_command(
                app,
                emit,
                get_passthrough_args=lambda: get_global_options().passthrough_args,
                get_global_harness=lambda: get_global_options().harness,
            )

        def _register_misc() -> None:
            from meridian.cli.misc_commands import register_misc_commands

            register_misc_commands(
                app=app,
                completion_app=completion_app,
                streaming_app=streaming_app,
                test_app=test_app,
                emit=emit,
                get_global_options=get_global_options,
            )

        def _register_sync() -> None:
            from meridian.cli.sync_cmd import register_sync_commands

            register_sync_commands(app, emit)

        def _register_chat() -> None:
            from meridian.cli.chat_cmd import register_chat_command

            register_chat_command(app)

        def _register_kg() -> None:
            import meridian.cli.kg_cmd as _kg_cmd

            _ = _kg_cmd

        def _register_mermaid() -> None:
            import meridian.cli.mermaid_cmd as _mermaid_cmd

            _ = _mermaid_cmd

        def _register_qi() -> None:
            import meridian.cli.qi_cmd as _qi_cmd

            _ = _qi_cmd

        def _register_report() -> None:
            from meridian.cli.report_cmd import register_report_commands

            register_report_commands(report_app, emit)

        def _register_migrate() -> None:
            from meridian.cli.migrate_cmd import register_migrate_command

            register_migrate_command(app, emit)

        registrations: dict[str, tuple[str, Callable[[], None]]] = {
            "spawn": ("spawn", _register_spawn),
            "session": ("session", _register_session),
            "work": ("work", _register_work),
            "config": ("config", _register_config),
            "hooks": ("hooks", _register_hooks),
            "models": ("models", _register_models),
            "ext": ("ext", _register_ext),
            "telemetry": ("telemetry", _register_telemetry),
            "workspace": ("workspace", _register_workspace),
            "doctor": ("doctor", _register_doctor),
            "bootstrap": ("bootstrap", _register_bootstrap),
            "completion": ("misc", _register_misc),
            "context": ("misc", _register_misc),
            "streaming": ("misc", _register_misc),
            "test": ("misc", _register_misc),
            "sync": ("sync", _register_sync),
            "chat": ("chat", _register_chat),
            "kg": ("kg", _register_kg),
            "mermaid": ("mermaid", _register_mermaid),
            "qi": ("qi", _register_qi),
            "report": ("report", _register_report),
            "migrate": ("migrate", _register_migrate),
        }

        registration = registrations.get(first_token or "")
        if registration is None:
            if _is_help_request(argv):
                for group_name, register_fn in set(registrations.values()):
                    _register_once(group_name, register_fn)
            # Root/default commands and decorator-registered commands (mars, serve, init)
            # do not need group registration. Unknown commands are rejected earlier.
        else:
            group_name, register_fn = registration
            _register_once(group_name, register_fn)
            if first_token == "spawn":
                _register_once("report", _register_report)

        if {group for group, _ in registrations.values()}.issubset(_registered_command_groups):
            _group_commands_registered = True

    if _is_help_request(argv):
        _apply_curated_help_for_registered_groups(agent_mode=agent_mode)


def _operation_error_message(exc: Exception) -> str:
    if isinstance(exc, KeyError) and exc.args:
        return str(exc.args[0])
    message = str(exc).strip()
    if message:
        return message
    return exc.__class__.__name__


def _emit_error(message: str, *, exit_code: int = 1) -> None:
    """Emit an error via the active sink and exit."""

    sink, flush_after = _resolve_sink(_GLOBAL_OPTIONS.get())
    sink.error(message, exit_code=exit_code)
    if flush_after:
        flush_sink(sink)
    raise SystemExit(exit_code)


def _top_level_command_names() -> set[str]:
    return COMMAND_CATALOG.top_level_names()


def _validate_top_level_command(argv: Sequence[str], *, global_harness: str | None = None) -> None:
    if _bootstrap_first_positional_token(argv) is None:
        return
    _bootstrap_validate_top_level_command(
        argv,
        known_commands=_top_level_command_names(),
        global_harness=global_harness,
    )


def _workspace_subcommand(argv: Sequence[str]) -> str | None:
    if not argv or argv[0] != "workspace":
        return None
    for token in argv[1:]:
        if token.startswith("-"):
            continue
        return token
    return None


def _bootstrap_setup_requested(argv: Sequence[str]) -> bool:
    if not argv or argv[0] != "bootstrap":
        return False

    for token in argv[1:]:
        if token == "--":
            break
        if token in {"--add", "--link"}:
            return True
        if token.startswith("--add=") or token.startswith("--link="):
            return True
    return False


def _known_child_commands(parent: str) -> set[str]:
    return {
        descriptor.command_path[1]
        for descriptor in COMMAND_CATALOG.all_descriptors()
        if len(descriptor.command_path) >= 2 and descriptor.command_path[0] == parent
    }


def _validate_workspace_subcommand(argv: Sequence[str]) -> None:
    subcommand = _workspace_subcommand(argv)
    if subcommand is None:
        return
    if subcommand in _known_child_commands("workspace"):
        return
    print(f"error: Unknown command: workspace {subcommand}", file=sys.stderr)
    raise SystemExit(1)


def _is_root_help_request(argv: Sequence[str]) -> bool:
    return _bootstrap_is_root_help_request(argv)


def _normalized_usage_command(argv: Sequence[str]) -> str:
    descriptor = classify_invocation(argv, COMMAND_CATALOG)
    if descriptor is None:
        return "root"
    return ".".join(descriptor.command_path) if descriptor.command_path else "root"


def _emit_usage_command_invoked(argv: Sequence[str]) -> None:
    emit_telemetry(
        "usage",
        "usage.command.invoked",
        scope="cli.dispatch",
        data={"command": _normalized_usage_command(argv)},
    )


def _install_cli_telemetry(
    *,
    telemetry_mode: StartupTelemetryMode | None,
    startup_class: StartupClass | None,
    project_root: Path | None,
) -> None:
    """Install CLI telemetry from descriptor policy after bootstrap resolution."""

    if telemetry_mode in {None, StartupTelemetryMode.NONE}:
        return
    try:
        from meridian.lib.telemetry.bootstrap import (
            TelemetryMode,
            TelemetryPlan,
            install,
        )

        if telemetry_mode == StartupTelemetryMode.STDERR:
            mode = TelemetryMode.STDERR
            runtime_root = None
        elif telemetry_mode == StartupTelemetryMode.SEGMENT:
            mode = TelemetryMode.SEGMENT
            if project_root is None:
                runtime_root = None
            else:
                from meridian.lib.state.paths import resolve_project_runtime_root_for_write

                runtime_root = resolve_project_runtime_root_for_write(project_root)
        else:
            # SEGMENT_OPTIONAL preserves the old behavior for project-write commands:
            # do not create user-runtime state solely to record optional telemetry.
            mode = TelemetryMode.SEGMENT
            runtime_root = None

        schedule_maintenance = (
            mode == TelemetryMode.SEGMENT
            and startup_class
            in {
                StartupClass.WRITE_RUNTIME,
                StartupClass.PRIMARY_LAUNCH,
            }
            and runtime_root is not None
        )
        install(
            TelemetryPlan(
                mode=mode,
                logical_owner=os.environ.get("MERIDIAN_SPAWN_ID") or "cli",
                runtime_root=runtime_root,
                schedule_maintenance=schedule_maintenance,
            )
        )
    except Exception:
        return


def _maybe_schedule_background_repairs(
    *,
    startup_class: StartupClass,
    project_root: Path | None,
    bootstrap_skipped: bool,
) -> None:
    """Schedule cheap per-project repairs on PRIMARY_LAUNCH in a daemon thread."""

    if bootstrap_skipped or startup_class != StartupClass.PRIMARY_LAUNCH or project_root is None:
        return

    from meridian.lib.ops.diag import schedule_background_repairs

    schedule_background_repairs(project_root)


def _print_agent_root_help() -> None:
    print(_AGENT_ROOT_HELP, end="")


@contextmanager
def _directory_env_scope(
    project_root: Path | None,
    directory_explicit: bool,
) -> Generator[None, None, None]:
    """Set ``MERIDIAN_PROJECT_DIR`` and restore on exit.

    When *directory_explicit* (``-C`` active), also unset ``MERIDIAN_RUNTIME_DIR``
    so the runtime layer derives from project identity instead of a stale override.
    """

    keys = ["MERIDIAN_PROJECT_DIR"]
    if directory_explicit:
        keys.append("MERIDIAN_RUNTIME_DIR")
    saved = {key: os.environ.get(key) for key in keys}
    if project_root is not None:
        os.environ["MERIDIAN_PROJECT_DIR"] = project_root.as_posix()
    if directory_explicit:
        os.environ.pop("MERIDIAN_RUNTIME_DIR", None)
    try:
        yield
    finally:
        for key in keys:
            prior = saved[key]
            if prior is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = prior


def main(argv: Sequence[str] | None = None) -> None:
    """CLI entry point used by `meridian` and `python -m meridian`."""

    with meridian_managed_env_default():
        _main_impl(argv=argv)


def _main_impl(argv: Sequence[str] | None = None) -> None:
    from meridian.lib.core.logging import configure_logging

    args = list(sys.argv[1:] if argv is None else argv)
    if _bootstrap_first_positional_token(args) != "mars":
        args = normalize_optional_value_flags(args)

    json_mode = "--json" in args
    if not json_mode and "--format" in args:
        try:
            fmt_idx = args.index("--format")
            fmt_val = args[fmt_idx + 1] if fmt_idx + 1 < len(args) else ""
            json_mode = fmt_val.lower() == "json"
        except (IndexError, ValueError):
            pass
    verbose_count = args.count("--verbose")
    configure_logging(json_mode=json_mode, verbosity=verbose_count)

    cleaned_args, options = _extract_global_options(args)
    if not (cleaned_args and cleaned_args[0] == "mars"):
        cleaned_args, passthrough_args = _split_passthrough_args(cleaned_args)
        options = options.model_copy(update={"passthrough_args": passthrough_args})
    if options.force_agent:
        effective_agent_mode = True
    elif options.force_human:
        effective_agent_mode = False
    else:
        effective_agent_mode = agent_mode_enabled() and not _interactive_terminal_attached()

    descriptor = classify_invocation(cleaned_args, COMMAND_CATALOG)

    # Resolve output format based on command and agent mode
    resolved_format = _resolve_output_format_for_command(
        argv=cleaned_args,
        explicit_format=options.explicit_format,
        agent_mode=effective_agent_mode,
    )
    suppress_events = effective_agent_mode and options.explicit_format is None
    options = options.model_copy(
        update={
            "output": OutputConfig(
                format=resolved_format,
                suppress_events=suppress_events,
            )
        }
    )

    if cleaned_args and cleaned_args[0] == "mars":
        _emit_usage_command_invoked(cleaned_args)
        with _directory_env_scope(options.project_root, options.directory_explicit):
            _run_mars_passthrough(cleaned_args[1:], output_format=options.output.format)

    if effective_agent_mode and (not cleaned_args or _is_root_help_request(cleaned_args)):
        _print_agent_root_help()
        return

    _validate_top_level_command(cleaned_args, global_harness=options.harness)
    _validate_workspace_subcommand(cleaned_args)

    # Handle descriptor-owned redirects before bootstrap work or lazy command registration.
    if descriptor is not None and descriptor.redirect is not None:
        print(
            "`meridian models list` has moved to Mars.\nUse `meridian mars models list` instead.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    startup_class = (
        descriptor.startup_class if descriptor is not None else StartupClass.PRIMARY_LAUNCH
    )
    help_request = _is_help_request(cleaned_args)
    bootstrap_skipped = help_request
    state_requirement = descriptor.state_requirement if descriptor is not None else None
    # bootstrap --add/--link must resolve root with init semantics in the handler
    # and reject invalid --dry-run combinations before any startup writes.
    if _bootstrap_setup_requested(cleaned_args):
        state_requirement = StateRequirement.NONE
    # Install options early so require_established_project_root() sees project_root
    # from -C / --directory during bootstrap resolution.
    _pre_bootstrap_token = _GLOBAL_OPTIONS.set(options)
    bootstrap_project_root = None
    with manual_hook_authority_scope(
        suppress=should_suppress_manual_hook_authority(argv=cleaned_args)
    ):
        if not bootstrap_skipped:
            bootstrap_project_root = maybe_bootstrap_runtime_state(
                cleaned_args,
                agent_mode=agent_mode_enabled(),
                state_requirement=state_requirement,
            )
    _GLOBAL_OPTIONS.reset(_pre_bootstrap_token)
    # Prefer bootstrap's resolved root; fall back to -C path if bootstrap was skipped/no-op.
    project_root = (
        bootstrap_project_root if bootstrap_project_root is not None else options.project_root
    )
    with _directory_env_scope(project_root, options.directory_explicit):
        _install_cli_telemetry(
            telemetry_mode=descriptor.telemetry_mode if descriptor is not None else None,
            startup_class=startup_class,
            project_root=project_root,
        )
        _maybe_schedule_background_repairs(
            startup_class=startup_class,
            project_root=project_root,
            bootstrap_skipped=bootstrap_skipped,
        )
        _emit_usage_command_invoked(cleaned_args)

        active_sink = create_sink(options.output)
        options = options.model_copy(update={"project_root": project_root, "sink": active_sink})
        token = _GLOBAL_OPTIONS.set(options)
        try:
            _register_commands_for_invocation(cleaned_args, agent_mode=effective_agent_mode)
            with temporary_config_env(options.config_file):
                try:
                    app(cleaned_args)
                except SystemExit:
                    raise
                except TimeoutError as exc:
                    _emit_error(_operation_error_message(exc), exit_code=124)
                except (KeyError, ValueError, FileNotFoundError, OSError, RuntimeError) as exc:
                    _emit_error(_operation_error_message(exc))
        finally:
            if help_request:
                _apply_curated_help_for_registered_groups(agent_mode=False)
            flush_sink(active_sink)
            _GLOBAL_OPTIONS.reset(token)
