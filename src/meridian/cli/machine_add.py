"""``meridian machine add``: save a machine after checking it can take a call.

Its order follows ``herdr machine add``. The steps need no terminal except where they say so;
a step that would ask a question, run without a terminal, prints what to run and asks nothing.
"""

from __future__ import annotations

import getpass
import json
import re
import shlex
import socket
import subprocess
import sys
import time
from contextlib import suppress
from dataclasses import dataclass
from typing import Final

from meridian import __version__
from meridian.cli.invocation import get_global_options
from meridian.cli.machine_forward import base_caller
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
    set_machine_name,
    ssh_target_problem,
    update_saved_machines,
)
from meridian.lib.utils.json_shapes import json_list, json_object

_INSTALL_SECONDS: Final = 300.0
_REACH_SECONDS: Final = 60.0
_BACK_TARGET_RE: Final = re.compile(r"[A-Za-z0-9._@:/-]+")
_UV_COMMAND: Final = "tool install --upgrade meridian-cli"
_UV_INSTALL_DOCS: Final = "docs.astral.sh/uv"
_REACH_SCRIPT: Final = """set -f
for cand in "$@"; do
  if ssh -T -o BatchMode=yes -o StrictHostKeyChecking=yes -o NumberOfPasswordPrompts=0 \\
      -o ConnectTimeout=5 -o LogLevel=ERROR -- "$cand" true </dev/null >/dev/null 2>&1; then
    echo "meridian-reach $cand"
  fi
done
"""


class AddError(Exception):
    """``machine add`` stops here; nothing is saved."""


def _is_terminal() -> bool:
    options = get_global_options()
    return sys.stdin.isatty() and sys.stderr.isatty() and not options.no_input


def _ask(question: str, *, default: bool) -> bool:
    """Ask yes or no in a terminal; ``--yes`` takes the default. An answer that is neither ends
    ``add``, as in herdr: Enter takes the default."""

    if get_global_options().yes:
        return default
    print(f"{question} [{'Y/n' if default else 'y/N'}] ", end="", file=sys.stderr, flush=True)
    answer = sys.stdin.readline().strip().lower()
    if not answer:
        return default
    if answer in ("y", "yes"):
        return True
    if answer in ("n", "no"):
        return False
    raise AddError("expected yes or no")


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
    if not _ask(question, default=not upgrade):
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


def _has_way_back(target: str, path: str, ready: _Ready, here: MachineIdentity) -> str | bool:
    """Step 8's check: the other side's saved list, looked at by this machine's ID. Returns the
    name it saves this machine under, or ``False`` when it doesn't."""

    result = call_machine(
        target,
        ["machine", "list", "--format", "json"],
        None,
        READ_LIMIT_SECONDS,
        ready.id,
        caller=base_caller(),
        binary=path,
    )
    if not isinstance(result, Answered) or result.exit != 0:
        return False
    try:
        document = json_object(json.loads(result.stdout)) or {}
    except ValueError:
        return False
    for entry in json_list(document.get("machines")) or []:
        fields = json_object(entry)
        if fields is not None and fields.get("id") == here.id:
            return str(fields.get("name"))
    return False


def _back_candidates(here: MachineIdentity) -> list[str]:
    """Targets that may reach this machine from the other: ``<login>@<hostname>``, the short
    name, and the addresses the host name resolves to."""

    try:
        login = getpass.getuser()
    except (KeyError, OSError):
        return []
    host = socket.gethostname()
    hosts = [host, host.split(".")[0]]
    with suppress(OSError):
        hosts += sorted({info[4][0] for info in socket.getaddrinfo(host, None, socket.AF_INET)})
    found = [f"{login}@{name}" for name in dict.fromkeys(hosts) if name and name != "localhost"]
    return [item for item in found if _BACK_TARGET_RE.fullmatch(item) and not item.startswith("-")]


def _answering_from_there(target: str, candidates: list[str]) -> list[str]:
    """The candidates the other machine can reach without a prompt and with a known host key,
    in order: only a test from there says what works from there."""

    if not candidates:
        return []
    process = SshProcess(target, ("/bin/sh", "-s", "--", *candidates))
    process.write(_REACH_SCRIPT.encode())
    process.close_stdin()
    deadline = time.monotonic() + _REACH_SECONDS
    reached: list[str] = []
    while True:
        line = process.read_line(deadline)
        if line is None:
            break
        if line is TIMED_OUT:
            process.kill()
            return reached
        assert isinstance(line, bytes)
        marker, _, value = line.decode("utf-8", "replace").strip().partition(" ")
        if marker == "meridian-reach" and value in candidates:
            reached.append(value)
    process.finish()
    return reached


def _offer_way_back(
    target: str, path: str, ready: _Ready, here: MachineIdentity, interactive: bool
) -> None:
    saved_as = _has_way_back(target, path, ready, here)
    if saved_as:
        text = f"`{ready.name}` already has this machine saved."
        if saved_as != here.name:
            text += f" It saves it as `{saved_as}`, and corrects that on its next call here."
        print(text)
        return
    reached = _answering_from_there(target, _back_candidates(here))
    suggestion = reached[0] if reached else None
    run_there = (
        f"meridian machine add {suggestion or '<SSH target that reaches this machine from there>'}"
    )
    if not interactive:
        print(
            f"Replies need `{ready.name}` to save this machine too. Run there: {run_there}",
        )
        return
    if not _ask(
        f"Also save this machine on {ready.name}, so its sessions can reply?", default=True
    ):
        print(f"Replies from {ready.name} need it. To do it later, run there: {run_there}")
        return
    default = suggestion or (_back_candidates(here) or [""])[0]
    if get_global_options().yes:
        back = default
    else:
        print(
            f"SSH target that reaches this machine from {ready.name} [{default}]: ",
            end="",
            file=sys.stderr,
            flush=True,
        )
        back = sys.stdin.readline().strip() or default
    if not back or not _BACK_TARGET_RE.fullmatch(back) or back.startswith("-"):
        print(f"No target that can be sent. To do it, run on {ready.name}: {run_there}")
        return
    status = subprocess.run(
        ["ssh", "-t", "--", target, path, "machine", "add", back], check=False
    ).returncode
    if status != 0:
        print(
            f"`machine add` on {ready.name} stopped (exit {status}); {ready.name} stays saved "
            f"here. To finish, run on {ready.name}: meridian machine add {shlex.quote(back)}"
        )


def add_machine_command(target: str) -> int:
    try:
        problem = ssh_target_problem(target)
        if problem is not None:
            raise AddError(problem)
        here = this_machine()
        interactive = _is_terminal()
        if interactive:
            # Step 2: ssh owns the terminal, so a password, a passphrase or a new host key is
            # answered in place by the person. Meridian types into nothing and stores none of it.
            signed_in = subprocess.run(["ssh", "--", target, "true"], check=False).returncode
            if signed_in != 0:
                raise AddError(f"ssh to {target} failed (exit {signed_in}); fix sign-in first")
        path = _find_or_install(target, interactive)
        ready = _read_ready(target, path)
        print(_save(ready, target, here))
        write_cache(ready.id, path=path, version=ready.version)
        _offer_way_back(target, path, ready, here, interactive)
    except AddError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except MachineError as exc:
        print(f"Error: {exc.text}", file=sys.stderr)
        return 1
    return 0


__all__ = ["add_machine_command"]
