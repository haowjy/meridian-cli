"""Which machine a name means: this one, or a saved one."""

from __future__ import annotations

from meridian.lib.state.machine_identity import (
    MachineIdentity,
    MachineIdentityError,
    read_or_create_machine_identity,
)
from meridian.lib.state.saved_machines import SavedMachine, SavedMachinesError, read_saved_machines


class MachineError(Exception):
    """A machine can't be named or reached; *kind* is one of the message error kinds."""

    def __init__(self, kind: str, text: str) -> None:
        super().__init__(text)
        self.kind = kind
        self.text = text


def this_machine() -> MachineIdentity:
    try:
        return read_or_create_machine_identity()
    except MachineIdentityError as exc:
        raise MachineError("failed", str(exc)) from exc


def resolve_machine(name: str) -> MachineIdentity | SavedMachine:
    """This machine's identity, or the saved machine *name* means.

    Order: this machine's name, a saved machine's name, one of this machine's former names,
    a saved machine's former name. A current name always beats a former one. The saved list
    is not read for this machine's own current name, so it works with a damaged list.
    """

    here = this_machine()
    if name == here.name:
        return here
    try:
        saved = read_saved_machines()
    except SavedMachinesError as exc:
        raise MachineError("failed", str(exc)) from exc
    for entry in saved:
        if entry.name == name:
            return here if entry.id == here.id else entry
    if name in here.was:
        return here
    former = [entry for entry in saved if name in entry.was]
    if len(former) > 1:
        names = " and ".join(f"`{entry.name}`" for entry in former)
        raise MachineError(
            "ambiguous",
            f"`{name}` is a former name of {names}; remove one with `meridian machine remove`",
        )
    if former:
        return here if former[0].id == here.id else former[0]
    raise MachineError(
        "not_found", f"unknown machine `{name}`; `meridian machine list` shows the saved ones"
    )


__all__ = ["MachineError", "resolve_machine", "this_machine"]
