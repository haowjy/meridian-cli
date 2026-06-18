"""CLI command handler for standalone doctor operation."""

from collections.abc import Callable
from typing import Annotated, Any

from cyclopts import App, Parameter

from meridian.cli.ext_registration import register_extension_cli_group
from meridian.lib.extensions.registry import get_first_party_registry
from meridian.lib.ops.diag import DoctorInput, doctor_sync

Emitter = Callable[[Any], None]


def _make_doctor_handler(emit: Emitter) -> Callable[..., None]:
    def handler(
        *,
        prune: Annotated[
            bool,
            Parameter(
                name="--prune", help="Delete stale spawn artifacts and telemetry retention targets."
            ),
        ] = False,
        global_: Annotated[
            bool,
            Parameter(
                name="--global",
                help="Include stale orphan project directories under the user state root.",
            ),
        ] = False,
        kill_orphans: Annotated[
            bool,
            Parameter(
                name="--kill-orphans",
                help="Terminate orphaned spawn process groups before finalizing stale runs.",
            ),
        ] = False,
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
) -> tuple[set[str], dict[str, str]]:
    handlers: dict[str, Callable[[], Callable[..., None]]] = {
        "meridian.doctor.doctor": lambda: _make_doctor_handler(emit),
    }
    return register_extension_cli_group(
        app,
        registry=get_first_party_registry(),
        group="doctor",
        handlers=handlers,
        emit=emit,
    )
