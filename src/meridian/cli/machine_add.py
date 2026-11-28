"""``meridian machine add``: save a machine after checking it can take a call.

Its order follows ``herdr machine add``. The steps need no terminal except where they say so;
a step that would ask a question, run without a terminal, prints what to run and asks nothing.
"""

from __future__ import annotations

import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Final

from meridian import __version__
from meridian.cli.machine_back import offer_way_back
from meridian.cli.machine_forward import base_caller, fail
from meridian.cli.machine_prompt import AddError, ask, is_terminal, say
from meridian.lib.machines.call import READ_LIMIT_SECONDS, call_machine, missing_text
from meridian.lib.machines.discover import Discovery, discover, write_cache
from meridian.lib.machines.resolve import MachineError, this_machine
from meridian.lib.machines.results import Answered, NotSent
from meridian.lib.machines.ssh import TIMED_OUT, SshProcess
from meridian.lib.state.machine_identity import MachineIdentity
from meridian.lib.state.saved_machines import (
    NameTaken,
    SavedMachine,
    SavedMachinesError,
    add_machine,
    read_saved_machines,
    set_machine_name,
    ssh_target_problem,
    update_saved_machines,
)

_INSTALL_SECONDS: Final = 300.0
_UV_COMMAND: Final = "tool install --upgrade meridian-cli"
_UV_INSTALL_DOCS: Final = "docs.astral.sh/uv"


def _install(target: str, uv: str) -> None:
    """Run ``uv tool install --upgrade meridian-cli`` on *target*, its output as it comes."""

    process = SshProcess(target, ("/bin/sh", "-s"))
    process.write(f'exec 2>&1\n"{uv}" {_UV_COMMAND}\n'.encode())
    process.close_stdin()
    deadline = time.monotonic() + _INSTALL_SECONDS
    tail: list[str] = []
    while True:
        line = process.read_line(deadline)
        if line is None:
            break
        if line is TIMED_OUT:
            process.kill()
            raise AddError(
                f"the install is still running on {target} or stuck; stopped, not retried"
            )
        assert isinstance(line, bytes)
        text = line.decode("utf-8", "replace").rstrip("\r\n")
        print(text, file=sys.stderr, flush=True)
        tail = [*tail, text][-5:]
    exited = process.finish()
    if exited.status != 0:
        raise AddError(
            f"uv failed on {target} ({exited.last_line or 'exit ' + str(exited.status)}); "
            f"last lines:\n  " + "\n  ".join(tail) + f"\nTo run it by hand: uv {_UV_COMMAND}"
        )


def _find_or_install(target: str, interactive: bool) -> str:
    """Steps 3 to 5: the path of a Meridian on *target* that takes calls, offering to install
    or upgrade one with ``uv`` when there is none."""

    found = discover(target)
    if isinstance(found, NotSent):
        raise AddError(found.text)
    if found.path is not None:
        return found.path
    if found.bad_path is not None:
        raise AddError(missing_text(target, found))
    upgrade = found.old is not None
    command = f"uv {_UV_COMMAND}"
    if found.uv is None:
        what = (
            f"Meridian on {target} is {found.old[1] or 'too old'}, and {__version__} is needed"
            if found.old is not None
            else f"no Meridian on {target}"
        )
        raise AddError(
            f"{what}, and no `uv` to install it with. Run there: install uv "
            f"({_UV_INSTALL_DOCS}), then `{command}`"
        )
    if not interactive:
        raise AddError(
            _wanted(found, target)
            + f"\nRun this on {target}, then run `machine add` again: {command}"
        )
    if upgrade:
        assert found.old is not None
        print(
            f"Meridian on {target} is {found.old[1] or 'too old'} (at {found.old[0]}); "
            f"{__version__} is needed. Sessions running there keep the old build until they "
            "are restarted.",
            file=sys.stderr,
        )
        question = "Upgrade it with uv?"
    else:
        question = f"Install Meridian on {target} with uv?"
    if not ask(question, default=not upgrade):
        raise AddError(_wanted(found, target))
    _install(target, found.uv)
    after = discover(target)
    if isinstance(after, NotSent):
        raise AddError(after.text)
    if after.path is None:
        if after.old is not None:
            installed = after.old[1] or "an unknown version"
            if upgrade:
                raise AddError(
                    f"the upgrade ran and the first Meridian found on {target} is still the old "
                    f"one ({after.old[0]}): it is not the install uv manages. Remove it, or link "
                    "the new one into ~/.local/bin"
                )
            raise AddError(
                f"uv installed Meridian {installed} on {target}, but {__version__} is needed: "
                "the release on PyPI is older than this build needs"
            )
        raise AddError(f"uv succeeded on {target} but no Meridian that takes calls was found")
    print(
        f"Upgraded Meridian on {target}. Sessions already running there were started by the "
        "old build; a sandboxed one can't run `meridian message` until it is restarted."
        if upgrade
        else f"Installed Meridian on {target}. It has no agents or model keys yet: reading and "
        "messaging don't need them, starting a session there does.",
        file=sys.stderr,
    )
    return after.path


def _wanted(found: Discovery, target: str) -> str:
    return missing_text(target, found)


@dataclass(frozen=True)
class _Ready:
    name: str
    id: str
    version: str


def _read_ready(target: str, path: str) -> _Ready:
    result = call_machine(
        target, ["machine", "name"], None, READ_LIMIT_SECONDS, binary=path, caller=base_caller()
    )
    if isinstance(result, NotSent):
        raise AddError(result.text)
    if not isinstance(result, Answered):
        raise AddError(f"{target} did not answer: {result.text}")
    return _Ready(result.machine, result.id, result.version)


def _save(ready: _Ready, target: str, here: MachineIdentity) -> str:
    """Step 7: one edit, decided against the list as it is under the lock. Returns what to say;
    raises ``AddError`` for the rows that stop."""

    if ready.id == here.id:
        raise AddError("that target is this machine")

    def edit(entries: list[SavedMachine], raw: list[object]) -> str:
        same_id = next((e for e in entries if e.id == ready.id), None)
        if same_id is not None:
            if same_id.ssh != target:
                raise AddError(
                    f"`{same_id.name}` is already saved with target {same_id.ssh}, and {target} "
                    "leads to the same machine. To change a target, run `meridian machine "
                    f"remove {same_id.name}` first"
                )
            if same_id.name == ready.name:
                return f"`{ready.name}` is already saved; checked again, nothing to change."
            outcome = set_machine_name(entries, raw, ready.id, ready.name, here.name)
            if isinstance(outcome, NameTaken):
                raise _name_taken(ready.name, outcome.holder, target)
            return f"`{same_id.name}` is now called `{ready.name}`; saved."
        at_target = next((e for e in entries if e.ssh == target), None)
        if at_target is not None:
            raise AddError(
                f"the machine at {target} is no longer the one saved as `{at_target.name}` (it "
                "was reinstalled, or lost its machine.json). Run `meridian machine remove "
                f"{at_target.name}` first, and `meridian machine rename {at_target.name}` on that "
                "machine if it should keep the name"
            )
        named = next((e for e in entries if e.name == ready.name), None)
        if named is not None or ready.name == here.name:
            holder = named.ssh if named is not None else "this machine"
            raise _name_taken(ready.name, holder, target)
        former = [e.name for e in entries if ready.name in e.was]
        add_machine(raw, SavedMachine(ready.name, target, ready.id))
        text = f"Saved `{ready.name}` ({target})."
        if former:
            text += (
                f" `{ready.name}` was a former name of `{former[0]}`: an old reference written "
                "with it now means this machine."
            )
        return text

    try:
        return update_saved_machines(edit)
    except SavedMachinesError as exc:
        raise AddError(str(exc)) from exc
    except OSError as exc:
        raise AddError(f"can't write the saved list: {exc}") from exc


def _name_taken(name: str, holder: str, target: str) -> AddError:
    return AddError(
        f"two machines have the name `{name}` ({holder} and {target}): run `meridian machine "
        "rename <new name>` on one, or `meridian machine remove` the old one"
    )


def add_machine_command(target: str) -> SavedMachine:
    """Save the machine at *target* and return its entry; a step that stops prints why and
    exits 1."""

    try:
        problem = ssh_target_problem(target)
        if problem is not None:
            raise AddError(problem)
        here = this_machine()
        interactive = is_terminal()
        if interactive:
            # Step 2: ssh owns the terminal, so a password, a passphrase or a new host key is
            # answered in place by the person. Meridian types into nothing and stores none of it.
            signed_in = subprocess.run(["ssh", "--", target, "true"], check=False).returncode
            if signed_in != 0:
                raise AddError(f"ssh to {target} failed (exit {signed_in}); fix sign-in first")
        path = _find_or_install(target, interactive)
        ready = _read_ready(target, path)
        say(_save(ready, target, here))
        write_cache(ready.id, path=path, version=ready.version)
        offer_way_back(target, path, ready.name, ready.id, here, interactive=interactive)
        saved = next((m for m in read_saved_machines() if m.id == ready.id), None)
        if saved is None:
            raise AddError(f"`{ready.name}` was removed from the saved list while it was added")
        return saved
    except (AddError, SavedMachinesError) as exc:
        fail(None, "failed", str(exc))
    except MachineError as exc:
        fail(None, exc.kind, exc.text)


__all__ = ["add_machine_command"]
