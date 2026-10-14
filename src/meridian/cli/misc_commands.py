"""Non-core command registrations for the main CLI router."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Literal, Protocol, cast

from cyclopts import App, Parameter

from meridian.cli.streaming_serve import streaming_serve


class _GlobalOptionsLike(Protocol):
    harness: str | None


def _normalize_completion_shell(shell: str) -> Literal["bash", "zsh", "fish"]:
    normalized = shell.strip().lower()
    if normalized not in {"bash", "zsh", "fish"}:
        raise ValueError("Unsupported shell. Expected one of: bash, zsh, fish.")
    return cast("Literal['bash', 'zsh', 'fish']", normalized)


def register_misc_commands(
    *,
    app: App,
    completion_app: App,
    context_app: App,
    streaming_app: App,
    test_app: App,
    emit: Callable[[object], None],
    get_global_options: Callable[[], _GlobalOptionsLike],
) -> None:
    def _emit_completion(shell: str) -> None:
        normalized = _normalize_completion_shell(shell)
        print(app.generate_completion(shell=normalized))

    def completion_bash() -> None:
        _emit_completion("bash")

    def completion_zsh() -> None:
        _emit_completion("zsh")

    def completion_fish() -> None:
        _emit_completion("fish")

    def completion_install(
        shell: Annotated[
            str,
            Parameter(
                name="--shell",
                help="Shell to generate completion for (bash, zsh, or fish).",
            ),
        ] = "bash",
        output: Annotated[
            str | None,
            Parameter(
                name="--output",
                help="Optional file path where completion script is written.",
            ),
        ] = None,
        add_to_startup: Annotated[
            bool,
            Parameter(
                name="--add-to-startup",
                help="Append completion setup to shell startup files.",
            ),
        ] = False,
    ) -> None:
        normalized_shell = _normalize_completion_shell(shell)
        destination = app.install_completion(
            shell=normalized_shell,
            output=Path(output).expanduser() if output is not None else None,
            add_to_startup=add_to_startup,
        )
        emit({"shell": normalized_shell, "path": destination.as_posix()})

    def streaming_serve_cmd(
        prompt: Annotated[
            str,
            Parameter(name=["--prompt", "-p"], help="Initial prompt for the streaming run."),
        ] = "",
        harness: Annotated[
            str | None,
            Parameter(name="--harness", help="Harness id: claude, codex, cursor, or opencode."),
        ] = None,
        model: Annotated[
            str | None,
            Parameter(name=["--model", "-m"], help="Optional model override."),
        ] = None,
        agent: Annotated[
            str | None,
            Parameter(name=["--agent", "-a"], help="Optional agent profile."),
        ] = None,
        debug: Annotated[
            bool,
            Parameter(name="--debug", help="Enable wire-level debug tracing."),
        ] = False,
    ) -> None:
        options = get_global_options()
        resolved_harness = (harness or options.harness or "").strip()
        if not resolved_harness:
            raise ValueError("harness required: pass --harness")
        if not prompt.strip():
            raise ValueError("prompt required: pass --prompt")
        asyncio.run(
            streaming_serve(
                harness=resolved_harness,
                prompt=prompt,
                model=model,
                agent=agent,
                debug=debug,
            )
        )

    def context_cmd(
        name: Annotated[
            str | None,
            Parameter(
                help=(
                    "Optional context name to print as an absolute path "
                    "(work = active work dir, kb, or work.archive)."
                ),
            ),
        ] = None,
        verbose: Annotated[
            bool,
            Parameter(
                name="--verbose",
                help="Show source/path/resolved details for each context.",
            ),
        ] = False,
    ) -> None:
        """Query configured context paths."""

        from meridian.lib.ops.context import ContextInput, context_sync

        output = context_sync(ContextInput(verbose=verbose))
        if name is None:
            emit(output)
            return
        emit(output.resolve_name(name))

    completion_app.command(completion_bash, name="bash")
    completion_app.command(completion_zsh, name="zsh")
    completion_app.command(completion_fish, name="fish")
    completion_app.command(completion_install, name="install")
    streaming_app.command(streaming_serve_cmd, name="serve")
    context_app.default(context_cmd)
