"""CLI command handler for standalone doctor operation."""

from collections.abc import Callable
from typing import Any

from cyclopts import App

from meridian.cli.ext_registration import register_extension_cli_group
from meridian.lib.extensions.registry import get_first_party_registry
from meridian.lib.ops.diag import DoctorInput, doctor_sync

Emitter = Callable[[Any], None]


def _make_doctor_handler(emit: Emitter) -> Callable[..., None]:
    def handler(
        *,
        prune: bool = False,
        global_: bool = False,
        kill_orphans: bool = False,
    ) -> None:
        emit(
            doctor_sync(
                DoctorInput(
                    prune=prune,
                    global_=global_,
                    kill_orphans=kill_orphans,
                )
            )
        )

    return handler


def register_doctor_command(
    app: App,
    emit: Emitter,
    *,
    agent_mode: bool = False,
) -> tuple[set[str], dict[str, str]]:
    base_epilogue = (
        "Health check and auto-repair for meridian state.\n\n"
        "Reconciles orphaned spawns (dead PIDs, missing spawn directories),\n"
        "cleans stale session locks, scans telemetry retention, and warns about\n"
        "missing or malformed configuration.\n\n"
        "Use --kill-orphans to terminate orphaned spawn process groups before\n"
        "finalizing stale runs.\n\n"
        "Use --prune to delete stale spawn artifacts, telemetry segments, and\n"
        "other stale retention targets for the current project.\n"
        "Add --global to also prune stale orphan project dirs globally\n"
        "across ~/.meridian/projects/.\n\n"
        "Doctor is idempotent - re-running converges on the same result.\n"
        "It is safe (and intended) to run after a crash, after a force-kill,\n"
        "or any time `meridian spawn show` reports a status that doesn't match\n"
        "reality.\n\n"
        "Examples:\n\n"
        "  meridian doctor                        # check and repair\n\n"
        "  meridian doctor --prune   # prune stale artifacts + telemetry\n\n"
        "  meridian doctor --prune --global      # also prune other stale projects\n\n"
        "  meridian doctor --kill-orphans        # terminate orphaned process groups\n\n"
        "  meridian doctor --format text          # human-readable summary\n"
    )
    if agent_mode:
        from meridian.cli.agent_help import agent_help_epilogue

        doctor_epilogue = agent_help_epilogue("doctor", base_epilogue)
    else:
        doctor_epilogue = base_epilogue

    handlers: dict[str, Callable[[], Callable[..., None]]] = {
        "meridian.doctor.doctor": lambda: _make_doctor_handler(emit),
    }
    return register_extension_cli_group(
        app,
        registry=get_first_party_registry(),
        group="doctor",
        handlers=handlers,
        command_help_epilogues={
            "meridian.doctor.doctor": doctor_epilogue or ""
        },
        emit=emit,
    )
