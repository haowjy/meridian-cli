"""``meridian machine``: save the machines this one can reach, and name this one."""

from __future__ import annotations

import json
from typing import Annotated, Never

from cyclopts import Parameter

from meridian.cli.app_tree import machine_app
from meridian.cli.invocation import get_global_options
from meridian.cli.machine_add import add_machine_command
from meridian.cli.machine_forward import base_caller, fail, forward_if_remote
from meridian.cli.machine_run import run_machine_call
from meridian.lib.core.refs import is_machine_name
from meridian.lib.machines.call import READ_LIMIT_SECONDS, call_machine
from meridian.lib.machines.discover import forget_cache
from meridian.lib.machines.resolve import MachineError, this_machine
from meridian.lib.machines.results import Answered, NotSent
from meridian.lib.state.machine_identity import (
    MachineIdentity,
    MachineIdentityError,
    rename_machine,
)
from meridian.lib.state.saved_machines import (
    SavedMachine,
    SavedMachinesError,
    read_saved_machines,
    remove_machine,
    update_saved_machines,
)


def _fail(text: str, kind: str = "failed") -> Never:
    fail(None, kind, text)


def _print(document: dict[str, object], text: str) -> None:
    """One object under ``--json`` (the shapes are in the group's help), else *text*."""

    if get_global_options().output.format == "json":
        print(json.dumps(document, separators=(",", ":"), sort_keys=True))
    else:
        print(text)


def _row(machine: SavedMachine) -> dict[str, object]:
    return {"name": machine.name, "ssh": machine.ssh, "id": machine.id, "was": list(machine.was)}


def _identity(here: MachineIdentity) -> dict[str, object]:
    return {"name": here.name, "id": here.id, "was": list(here.was)}


def _not_saved(name: str) -> Never:
    _fail(
        f"no machine is saved as `{name}`; `meridian machine list` shows the saved ones",
        "not_found",
    )


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
    asked in a terminal only; `--yes` answers each with its default. With `--json` it prints
    the saved machine, `{"name", "ssh", "id", "was"}`, and what it has to say goes to stderr."""

    saved = add_machine_command(target)
    if get_global_options().output.format == "json":
        print(json.dumps(_row(saved), separators=(",", ":"), sort_keys=True))


@machine_app.command(name="list")
def machine_list() -> None:
    """Print the saved machines: name, SSH target, ID and former names. With `--json`:
    `{"machines": [{"name", "ssh", "id", "was"}]}`."""

    forward_if_remote()
    try:
        machines = read_saved_machines()
    except SavedMachinesError as exc:
        _fail(str(exc))
    width = max((len(m.name) for m in machines), default=0)
    lines = [
        f"{m.name:<{width}}  {m.ssh}  {m.id}" + (f"  (was {', '.join(m.was)})" if m.was else "")
        for m in machines
    ]
    _print(
        {"machines": [_row(m) for m in machines]},
        "\n".join(lines) or "No machines saved. `meridian machine add <ssh target>` saves one.",
    )


@machine_app.command(name="remove")
def machine_remove(
    name: Annotated[str, Parameter(show_default=False, help="A saved machine's name.")],
) -> None:
    """Forget a saved machine and its cache. Nothing changes on that machine. With `--json`
    it prints the machine it forgot: `{"name", "ssh", "id", "was"}`."""

    def edit(entries: list[SavedMachine], raw: list[object]) -> SavedMachine | None:
        found = next((entry for entry in entries if entry.name == name), None)
        if found is not None:
            remove_machine(raw, name)
        return found

    try:
        removed = update_saved_machines(edit)
    except (SavedMachinesError, OSError) as exc:
        _fail(str(exc))
    if removed is None:
        _not_saved(name)
    forget_cache(removed.id)
    _print(_row(removed), f"Removed `{name}`.")


@machine_app.command(name="status")
def machine_status(
    name: Annotated[
        str | None, Parameter(show_default=False, help="One saved machine; default: all.")
    ] = None,
) -> None:
    """Make one call to each named machine and print its name, version and what failed. With
    `--json`: `{"machines": [{"name", "ok": true, "version"} or {"name", "ok": false,
    "error": {"kind", "text"}}]}`. Exits 1 when any call failed."""

    try:
        machines = read_saved_machines()
    except SavedMachinesError as exc:
        _fail(str(exc))
    chosen = [m for m in machines if name is None or m.name == name]
    if name is not None and not chosen:
        _not_saved(name)
    json_mode = get_global_options().output.format == "json"
    rows: list[dict[str, object]] = []
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
            rows.append({"name": machine.name, "ok": True, "version": result.version})
            line = f"{machine.name}  ok  meridian {result.version}"
        else:
            kind = result.kind if isinstance(result, NotSent) else "unreachable"
            rows.append(
                {"name": machine.name, "ok": False, "error": {"kind": kind, "text": result.text}}
            )
            line = f"{machine.name}  {kind}  {result.text}"
        if not json_mode:
            print(line)
    if json_mode:
        print(json.dumps({"machines": rows}, separators=(",", ":"), sort_keys=True))
    raise SystemExit(0 if all(row["ok"] for row in rows) else 1)


def _caller() -> dict[str, object]:
    try:
        return base_caller()
    except MachineError as exc:
        _fail(exc.text, exc.kind)


@machine_app.command(name="name")
def machine_name() -> None:
    """Print this machine's name, the one other machines write in front of an ID. With
    `--json`: `{"name", "id", "was"}`, the ID that never changes and the former names."""

    forward_if_remote()
    try:
        here = this_machine()
    except MachineError as exc:
        _fail(exc.text, exc.kind)
    _print(_identity(here), here.name)


@machine_app.command(name="rename")
def machine_rename(
    new_name: Annotated[str, Parameter(show_default=False, help="[a-z][a-z0-9-]{0,62}")],
) -> None:
    """Rename this machine. Machines that saved it correct the name on their next call here, and
    a reference written with the old name still resolves. Refuses a name another saved machine
    has. With `--json` it prints what `machine name --json` then prints."""

    if not is_machine_name(new_name):
        _fail(
            f"`{new_name}` can't be a machine name: it must match [a-z][a-z0-9-]{{0,62}}, and "
            "not be `inbox` or look like a spawn or chat ID (p123, c123)",
            "usage",
        )
    try:
        here = this_machine()
        taken = next(
            (m for m in read_saved_machines() if m.name == new_name and m.id != here.id), None
        )
        if taken is not None:
            _fail(
                f"`{new_name}` is the name of a saved machine ({taken.ssh}); pick another",
                "refused",
            )
        renamed = rename_machine(new_name)
    except (MachineError, SavedMachinesError, MachineIdentityError) as exc:
        _fail(str(exc))
    _print(_identity(renamed), f"This machine is now `{renamed.name}`.")


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
