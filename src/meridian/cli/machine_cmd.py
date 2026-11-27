"""``meridian machine``: save the machines this one can reach, and name this one."""

from __future__ import annotations

import json
import sys
from typing import Annotated, Never

from cyclopts import Parameter

from meridian.cli.app_tree import machine_app
from meridian.cli.invocation import get_global_options
from meridian.cli.machine_add import add_machine_command
from meridian.cli.machine_forward import base_caller, forward_if_remote
from meridian.cli.machine_run import run_machine_call
from meridian.lib.core.refs import is_machine_name
from meridian.lib.machines.call import READ_LIMIT_SECONDS, call_machine
from meridian.lib.machines.discover import forget_cache
from meridian.lib.machines.resolve import MachineError, this_machine
from meridian.lib.machines.results import Answered, NotSent
from meridian.lib.state.machine_identity import MachineIdentityError, rename_machine
from meridian.lib.state.saved_machines import (
    SavedMachine,
    SavedMachinesError,
    read_saved_machines,
    remove_machine,
    update_saved_machines,
)


def _fail(text: str) -> Never:
    print(f"error: {text}", file=sys.stderr)
    raise SystemExit(1)


@machine_app.command(name="add")
def machine_add(
    target: Annotated[
        str,
        Parameter(
            show_default=False,
            help="A Host from ~/.ssh/config, user@host, or ssh://user@host:port.",
        ),
    ],
) -> None:
    """Save a machine: check it can take a call, offer to install or upgrade Meridian there,
    and offer to save this machine there too, so its sessions can reply. The questions are
    asked in a terminal only; `--yes` answers each with its default."""

    raise SystemExit(add_machine_command(target))


@machine_app.command(name="list")
def machine_list() -> None:
    """Print the saved machines: name, SSH target, ID and former names."""

    forward_if_remote()
    try:
        machines = read_saved_machines()
    except SavedMachinesError as exc:
        _fail(str(exc))
    if get_global_options().output.format == "json":
        document = {
            "machines": [
                {"name": m.name, "ssh": m.ssh, "id": m.id, "was": list(m.was)} for m in machines
            ]
        }
        print(json.dumps(document, separators=(",", ":"), sort_keys=True))
        return
    if not machines:
        print("No machines saved. `meridian machine add <ssh target>` saves one.")
        return
    width = max(len(m.name) for m in machines)
    for m in machines:
        former = f"  (was {', '.join(m.was)})" if m.was else ""
        print(f"{m.name:<{width}}  {m.ssh}  {m.id}{former}")


@machine_app.command(name="remove")
def machine_remove(
    name: Annotated[str, Parameter(show_default=False, help="A saved machine's name.")],
) -> None:
    """Forget a saved machine and its cache. Nothing changes on that machine."""

    def edit(entries: list[SavedMachine], raw: list[object]) -> str | None:
        found = next((entry for entry in entries if entry.name == name), None)
        if found is None:
            return None
        remove_machine(raw, name)
        return found.id

    try:
        machine_id = update_saved_machines(edit)
    except (SavedMachinesError, OSError) as exc:
        _fail(str(exc))
    if machine_id is None:
        _fail(f"no machine is saved as `{name}`; `meridian machine list` shows the saved ones")
    forget_cache(machine_id)
    print(f"Removed `{name}`.")


@machine_app.command(name="status")
def machine_status(
    name: Annotated[
        str | None, Parameter(show_default=False, help="One saved machine; default: all.")
    ] = None,
) -> None:
    """Make one call to each named machine and print its name, version and what failed."""

    try:
        machines = read_saved_machines()
    except SavedMachinesError as exc:
        _fail(str(exc))
    chosen = [m for m in machines if name is None or m.name == name]
    if name is not None and not chosen:
        _fail(f"no machine is saved as `{name}`; `meridian machine list` shows the saved ones")
    failed = False
    for machine in chosen:
        result = call_machine(
            machine.ssh,
            ["machine", "name"],
            None,
            READ_LIMIT_SECONDS,
            machine.id,
            caller=_caller(),
            label=machine.name,
        )
        if isinstance(result, Answered):
            print(f"{machine.name}  ok  meridian {result.version}")
        elif isinstance(result, NotSent):
            failed = True
            print(f"{machine.name}  {result.kind}  {result.text}")
        else:
            failed = True
            print(f"{machine.name}  unreachable  {result.text}")
    raise SystemExit(1 if failed else 0)


def _caller() -> dict[str, object]:
    try:
        return base_caller()
    except MachineError as exc:
        _fail(exc.text)


@machine_app.command(name="name")
def machine_name() -> None:
    """Print this machine's name, the one other machines write in front of an ID."""

    forward_if_remote()
    try:
        print(this_machine().name)
    except MachineError as exc:
        _fail(exc.text)


@machine_app.command(name="rename")
def machine_rename(
    new_name: Annotated[str, Parameter(show_default=False, help="[a-z][a-z0-9-]{0,62}")],
) -> None:
    """Rename this machine. Machines that saved it correct the name on their next call here, and
    a reference written with the old name still resolves. Refuses a name another saved machine
    has."""

    if not is_machine_name(new_name):
        _fail(
            f"`{new_name}` can't be a machine name: it must match [a-z][a-z0-9-]{{0,62}}, and "
            "not be `inbox` or look like a spawn or chat ID (p123, c123)"
        )
    try:
        here = this_machine()
        taken = next(
            (m for m in read_saved_machines() if m.name == new_name and m.id != here.id), None
        )
        if taken is not None:
            _fail(f"`{new_name}` is the name of a saved machine ({taken.ssh}); pick another")
        renamed = rename_machine(new_name)
    except (MachineError, SavedMachinesError, MachineIdentityError) as exc:
        _fail(exc.text if isinstance(exc, MachineError) else str(exc))
    print(f"This machine is now `{renamed.name}`.")


@machine_app.command(name="run", show=False)
def machine_run(
    *,
    check: Annotated[
        bool, Parameter(name="--check", help="Print one fixed word and exit.")
    ] = False,
) -> None:
    """The receiving end of a call from another machine. Not typed by a person."""

    run_machine_call(check=check)


__all__ = []
