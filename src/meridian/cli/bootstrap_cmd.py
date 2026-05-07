"""Top-level `meridian bootstrap` command."""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated

from cyclopts import App, Parameter


def register_bootstrap_command(
    app: App,
    emit: Callable[[object], None],
    get_passthrough_args: Callable[[], tuple[str, ...]],
    get_global_harness: Callable[[], str | None],
) -> None:
    @app.command(name="bootstrap")
    def bootstrap(  # pyright: ignore[reportUnusedFunction]
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
            Parameter(name=["--agent", "-a"], help="Agent profile name for bootstrap."),
        ] = None,
        work: Annotated[
            str,
            Parameter(name="--work", help="Attach the bootstrap session to a work item id."),
        ] = "",
        yolo: Annotated[
            bool,
            Parameter(name="--yolo", help="Skip harness safety prompts."),
        ] = False,
        approval: Annotated[
            str | None,
            Parameter(name="--approval", help="Approval mode: default, confirm, auto, yolo."),
        ] = None,
        autocompact: Annotated[
            int | None,
            Parameter(name="--autocompact", help="Autocompact threshold percentage."),
        ] = None,
        effort: Annotated[
            str | None,
            Parameter(name="--effort", help="Effort level: low, medium, high, xhigh."),
        ] = None,
        sandbox: Annotated[
            str | None,
            Parameter(name="--sandbox", help="Sandbox mode."),
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
        """Launch a primary agent session with installed bootstrap docs."""

        from meridian.cli import primary_launch

        if yolo and approval is not None:
            raise ValueError("Cannot combine --yolo with --approval.")

        explicit_harness = harness.strip() if harness is not None and harness.strip() else None
        global_harness = get_global_harness()
        if global_harness and explicit_harness and global_harness != explicit_harness:
            raise ValueError(
                f"Conflicting harness selections: '{global_harness}' and '{explicit_harness}'."
            )

        emit(
            primary_launch.run_primary_launch(
                continue_ref=None,
                fork_ref=None,
                model=model,
                harness=global_harness or explicit_harness,
                agent=agent,
                work=work,
                yolo=yolo,
                approval=approval,
                autocompact=autocompact,
                effort=effort,
                sandbox=sandbox,
                timeout=timeout,
                dry_run=dry_run,
                passthrough=get_passthrough_args(),
                include_bootstrap_documents=True,
            )
        )


__all__ = ["register_bootstrap_command"]
