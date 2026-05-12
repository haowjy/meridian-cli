"""Cyclopts CLI entry point for meridian."""

import os
import subprocess
import sys
from collections.abc import Callable, Sequence
from contextvars import ContextVar
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, cast

from cyclopts import Parameter
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
from meridian.cli.startup.policy import StartupClass
from meridian.cli.startup.policy import TelemetryMode as StartupTelemetryMode
from meridian.lib.core.depth import is_nested_meridian_process, is_root_side_effect_process
from meridian.lib.core.sink import OutputSink
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


def emit(payload: object) -> None:
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
        )
    else:
        emit_output(payload, sink=sink)
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
    return is_nested_meridian_process()


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

    return resolve_effective_format(
        explicit_format=explicit_format,
        agent_mode=agent_mode,
        agent_default_format=agent_default_format,
    )


def _interactive_terminal_attached() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


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
                "Fork from a session ref: chat id (c123), spawn id (p123), "
                "or raw harness session id."
            ),
        ),
    ] = None,
    model: Annotated[
        str,
        Parameter(name=["--model", "-m"], help="Model id or alias for primary harness."),
    ] = "",
    harness: Annotated[
        str | None,
        Parameter(name="--harness", help="Force harness id (claude, codex, or opencode)."),
    ] = None,
    agent: Annotated[
        str | None,
        Parameter(name=["--agent", "-a"], help="Agent profile name for the primary agent."),
    ] = None,
    work: Annotated[
        str,
        Parameter(name="--work", help="Attach the primary session to a work item id."),
    ] = "",
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
        Parameter(name="--effort", help="Effort level: low, medium, high, xhigh."),
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
            help="Approval mode: default, confirm, auto, yolo. Overrides agent profile.",
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

    _run_primary_launch(
        continue_ref=continue_ref,
        fork_ref=fork_ref,
        model=model,
        harness=global_harness or explicit_harness,
        agent=agent,
        work=work,
        yolo=yolo,
        approval=approval,
        autocompact=autocompact,
        autocompact_pct=autocompact_pct,
        effort=effort,
        sandbox=sandbox,
        timeout=timeout,
        dry_run=dry_run,
        # See _split_passthrough_args() for why this reads from GlobalOptions.
        passthrough=get_global_options().passthrough_args,
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


def _run_primary_launch(
    *,
    continue_ref: str | None,
    fork_ref: str | None,
    model: str,
    harness: str | None,
    agent: str | None,
    work: str,
    yolo: bool,
    approval: str | None,
    autocompact: int | None,
    autocompact_pct: int | None,
    effort: str | None,
    sandbox: str | None,
    timeout: float | None,
    dry_run: bool,
    passthrough: tuple[str, ...],
) -> None:
    from meridian.cli import primary_launch

    emit(
        primary_launch.run_primary_launch(
            continue_ref=continue_ref,
            fork_ref=fork_ref,
            model=model,
            harness=harness,
            agent=agent,
            work=work,
            yolo=yolo,
            approval=approval,
            autocompact=autocompact,
            autocompact_pct=autocompact_pct,
            effort=effort,
            sandbox=sandbox,
            timeout=timeout,
            dry_run=dry_run,
            passthrough=passthrough,
        )
    )


def _run_init_link_flow_json(
    *,
    executable: str,
    mars_mode: str,
    mars_args: Sequence[str],
    link: str,
    config_result: BaseModel,
) -> None:
    from meridian.cli import mars_passthrough

    return mars_passthrough.run_init_link_flow_json(
        executable=executable,
        mars_mode=mars_mode,
        mars_args=mars_args,
        link=link,
        config_result=config_result,
        emit=emit,
        parse_request=mars_passthrough.parse_mars_passthrough,
        execute_request=_execute_mars_passthrough,
        decode_values=mars_passthrough.decode_json_values,
    )


@app.command(name="init")
def init_alias(
    path: Annotated[
        str | None,
        Parameter(name="path", help="Optional project path to initialize."),
    ] = None,
    link: Annotated[
        str | None,
        Parameter(
            name="--link",
            help="Link .mars/ into tool directory after config bootstrap (for example .claude).",
        ),
    ] = None,
) -> None:
    """Initialize meridian in the current project or provided path."""

    from meridian.cli import mars_passthrough
    from meridian.lib.ops.config import ConfigInitInput, config_init_sync

    project_root = mars_passthrough.resolve_init_project_root(path)
    result = config_init_sync(ConfigInitInput(project_root=project_root.as_posix()))
    if link is None:
        emit(result)
        return

    mars_mode, mars_args = mars_passthrough.resolve_init_link_mars_command(project_root, link)
    output_format = get_global_options().output.format
    if output_format == "json":
        executable = mars_passthrough.resolve_mars_executable()
        if executable is None:
            print(
                "error: Failed to execute 'mars'. Install meridian with dependencies and retry.",
                file=sys.stderr,
            )
            raise SystemExit(1)
        _run_init_link_flow_json(
            executable=executable,
            mars_mode=mars_mode,
            mars_args=mars_args,
            link=link,
            config_result=result,
        )
        return
    _run_mars_passthrough(mars_args, output_format=output_format)


def _first_command_token(argv: Sequence[str]) -> str | None:
    for arg in argv:
        if not arg.startswith("-"):
            return arg
    return None


def _register_commands_for_invocation(
    argv: Sequence[str],
    *,
    agent_mode: bool = False,
) -> None:
    """Register only the command group needed for the current invocation."""

    global _group_commands_registered
    if _group_commands_registered:
        return

    first_token = _first_command_token(argv)

    def _register_agent_epilogue(command: str) -> None:
        if not agent_mode:
            return
        from meridian.cli.agent_help import agent_help_epilogue

        if command == "spawn":
            spawn_app.help_epilogue = agent_help_epilogue("spawn", spawn_app.help_epilogue)
        elif command == "session":
            session_app.help_epilogue = agent_help_epilogue("session", session_app.help_epilogue)
        elif command == "config":
            config_app.help_epilogue = agent_help_epilogue("config", config_app.help_epilogue)
        elif command == "work":
            work_app.help_epilogue = agent_help_epilogue("work", work_app.help_epilogue)

    def _register_once(name: str, register: Callable[[], None]) -> None:
        if name in _registered_command_groups:
            return
        register()
        _registered_command_groups.add(name)

    def _register_spawn() -> None:
        from meridian.cli.spawn import register_spawn_commands

        _register_agent_epilogue("spawn")
        register_spawn_commands(spawn_app, emit)

    def _register_session() -> None:
        from meridian.cli.session_cmd import register_session_commands

        _register_agent_epilogue("session")
        register_session_commands(session_app, emit)

    def _register_work() -> None:
        from meridian.cli.work_cmd import register_work_commands

        _register_agent_epilogue("work")
        register_work_commands(work_app, emit)

    def _register_config() -> None:
        from meridian.cli.config_cmd import register_config_commands

        _register_agent_epilogue("config")
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

        register_doctor_command(app, emit, agent_mode=agent_mode)

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
        "chat": ("chat", _register_chat),
        "kg": ("kg", _register_kg),
        "mermaid": ("mermaid", _register_mermaid),
        "qi": ("qi", _register_qi),
        "report": ("report", _register_report),
        "migrate": ("migrate", _register_migrate),
    }

    registration = registrations.get(first_token or "")
    if registration is None:
        if any(arg in {"--help", "-h"} for arg in argv):
            for group_name, register_fn in set(registrations.values()):
                _register_once(group_name, register_fn)
        # Root/default commands and decorator-registered commands (mars, serve, init)
        # do not need group registration. Unknown commands are rejected earlier.
        return

    group_name, register_fn = registration
    _register_once(group_name, register_fn)
    if first_token == "spawn":
        _register_once("report", _register_report)

    if {group for group, _ in registrations.values()}.issubset(_registered_command_groups):
        _group_commands_registered = True


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

    if bootstrap_skipped:
        return
    if startup_class != StartupClass.PRIMARY_LAUNCH:
        return
    if project_root is None:
        return
    if not is_root_side_effect_process():
        return

    import threading

    def _run_repairs() -> None:
        from contextlib import suppress

        with suppress(Exception):
            from meridian.lib.ops.diag import (
                _repair_orphan_runs,  # pyright: ignore[reportPrivateUsage]
                _repair_stale_session_locks,  # pyright: ignore[reportPrivateUsage]
            )

            _repair_stale_session_locks(project_root)
            _repair_orphan_runs(project_root)

    thread = threading.Thread(
        target=_run_repairs,
        name="meridian-background-repairs",
        daemon=True,
    )
    thread.start()


def _print_agent_root_help() -> None:
    print(_AGENT_ROOT_HELP, end="")


def main(argv: Sequence[str] | None = None) -> None:
    """CLI entry point used by `meridian` and `python -m meridian`."""

    from meridian.lib.core.logging import configure_logging

    args = list(sys.argv[1:] if argv is None else argv)

    json_mode = "--json" in args
    if not json_mode and "--format" in args:
        try:
            fmt_idx = args.index("--format")
            fmt_val = args[fmt_idx + 1] if fmt_idx + 1 < len(args) else ""
            json_mode = fmt_val.lower() == "json"
        except (IndexError, ValueError):
            pass
    verbose_count = args.count("--verbose") + args.count("-v")
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
    bootstrap_skipped = any(arg in {"--help", "-h"} for arg in cleaned_args)
    state_requirement = descriptor.state_requirement if descriptor is not None else None
    project_root = None
    with manual_hook_authority_scope(
        suppress=should_suppress_manual_hook_authority(argv=cleaned_args)
    ):
        if not bootstrap_skipped:
            project_root = maybe_bootstrap_runtime_state(
                cleaned_args,
                agent_mode=agent_mode_enabled(),
                state_requirement=state_requirement,
            )
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
        flush_sink(active_sink)
        _GLOBAL_OPTIONS.reset(token)
