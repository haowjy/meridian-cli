"""The way back: ``machine add``'s last step saves this machine on the other one, so the
sessions there can reply.

A reply is a call in the other direction, so the other machine needs an SSH target that
reaches this one. Only a test run from the other machine says what works from there, so the
candidates are tried there (``BatchMode``, ``StrictHostKeyChecking=yes``) and the first that
answers is offered. When none does there is no default.
"""

from __future__ import annotations

import getpass
import glob
import ipaddress
import json
import re
import shlex
import socket
import subprocess
import time
from contextlib import suppress
from typing import Final

from meridian.cli.machine_forward import base_caller
from meridian.cli.machine_prompt import ask, ask_text
from meridian.lib.machines.call import READ_LIMIT_SECONDS, call_machine
from meridian.lib.machines.results import Answered
from meridian.lib.machines.ssh import TIMED_OUT, SshProcess
from meridian.lib.state.machine_identity import MachineIdentity
from meridian.lib.utils.json_shapes import json_list, json_object

_REACH_SECONDS: Final = 60.0
_TARGET_RE: Final = re.compile(r"[A-Za-z0-9._@:/-]+")
_WORD_RE: Final = re.compile(r"[A-Za-z0-9._-]+")
_KEY_RE: Final = re.compile(r"[A-Za-z0-9+/=]+")
_BRIDGE_PREFIXES: Final = ("docker", "br-", "veth", "virbr", "lo", "cni", "flannel")
# Everything the script needs comes in as quoted words checked here; it is sent to /bin/sh -s on
# stdin, so no word reaches a remote command line.
_REACH_SCRIPT: Final = """set -f
login='@LOGIN@'
keys='@KEYS@'
cands='@CANDS@'
try() {
  if ssh -T -o BatchMode=yes -o StrictHostKeyChecking=yes -o NumberOfPasswordPrompts=0 \\
      -o ConnectTimeout=5 -o LogLevel=ERROR -- "$1" true </dev/null >/dev/null 2>&1; then
    echo "meridian-reach $1"
  fi
}
if [ -r "$HOME/.ssh/known_hosts" ]; then
  for k in $keys; do
    awk -v k="$k" '$1 !~ /^[@|]/ && ($2 == k || $3 == k) { print $1 }' "$HOME/.ssh/known_hosts"
  done | tr ',' '\\n' | sed -e 's/^\\[//' -e 's/\\]:[0-9]*$//' | sort -u | while read -r h; do
    [ -n "$h" ] && try "$login@$h"
  done
fi
for c in $cands; do try "$c"; done
"""


def valid_target(value: str) -> bool:
    return bool(_TARGET_RE.fullmatch(value)) and not value.startswith("-")


def _bridge_addresses() -> set[str]:
    """This machine's addresses on bridge and loopback interfaces: from the other machine they
    lead to itself. Linux only; elsewhere nothing is filtered by name."""

    try:
        out = subprocess.run(
            ["ip", "-o", "-4", "addr"], capture_output=True, text=True, timeout=5, check=False
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return set()
    found: set[str] = set()
    for line in out.splitlines():
        words = line.split()
        if len(words) > 3 and words[1].startswith(_BRIDGE_PREFIXES) and words[2] == "inet":
            found.add(words[3].partition("/")[0])
    return found


def back_candidates() -> list[str]:
    """Targets that may reach this machine from the other: ``<login>@<hostname>``, the short
    name, and the usable addresses the host name resolves to."""

    try:
        login = getpass.getuser()
    except (KeyError, OSError):
        return []
    host = socket.gethostname()
    hosts = [host, host.split(".")[0]]
    with suppress(OSError):
        skip = _bridge_addresses()
        for info in socket.getaddrinfo(host, None, socket.AF_INET):
            address = ipaddress.ip_address(info[4][0])
            if not (address.is_loopback or address.is_link_local) and str(address) not in skip:
                hosts.append(str(address))
    found = [f"{login}@{name}" for name in dict.fromkeys(hosts) if name != "localhost"]
    return [item for item in found if valid_target(item)]


def _host_keys() -> list[str]:
    """The public host keys of this machine: the far side's ``known_hosts`` names it by one."""

    keys: list[str] = []
    for path in sorted(glob.glob("/etc/ssh/ssh_host_*_key.pub")):
        with suppress(OSError):
            words = open(path, encoding="utf-8").read().split()  # noqa: SIM115
            if len(words) > 1 and _KEY_RE.fullmatch(words[1]):
                keys.append(words[1])
    return keys[:4]


def answering_from_there(target: str, candidates: list[str]) -> list[str]:
    """What the other machine can reach without a prompt and with a known host key, in order:
    names its ``known_hosts`` holds for this machine's own key first (that is what finds a
    name like ``homebox``), then *candidates*."""

    try:
        login = getpass.getuser()
    except (KeyError, OSError):
        return []
    if not _WORD_RE.fullmatch(login) or not all(valid_target(c) for c in candidates):
        return []
    script = (
        _REACH_SCRIPT.replace("@LOGIN@", login)
        .replace("@KEYS@", " ".join(_host_keys()))
        .replace("@CANDS@", " ".join(candidates))
    )
    try:
        process = SshProcess(target, ("/bin/sh", "-s"))
    except OSError:
        return []
    with suppress(OSError):
        process.write(script.encode("utf-8"))
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
        if marker == "meridian-reach" and valid_target(value) and value not in reached:
            reached.append(value)
    process.finish()
    return reached


def _far_machines(target: str, path: str, ready_id: str) -> list[dict[str, object]] | None:
    """The other side's saved list, or ``None`` when it can't be read."""

    result = call_machine(
        target,
        ["machine", "list", "--format", "json"],
        None,
        READ_LIMIT_SECONDS,
        ready_id,
        caller=base_caller(),
        binary=path,
    )
    if not isinstance(result, Answered) or result.exit != 0:
        return None
    try:
        document = json_object(json.loads(result.stdout)) or {}
    except ValueError:
        return None
    entries = (json_object(item) for item in json_list(document.get("machines")) or [])
    return [entry for entry in entries if entry is not None]


def _saved_there_as(entries: list[dict[str, object]] | None, here: MachineIdentity) -> str | None:
    for entry in entries or []:
        if entry.get("id") == here.id:
            return str(entry.get("name"))
    return None


def _report(
    saved_as: str | None,
    far: str,
    here: MachineIdentity,
    entries: list[dict[str, object]] | None,
    back: str,
) -> None:
    """The three outcomes: saved there, saved under another name, or not saved."""

    if saved_as == here.name:
        print(f"`{far}` has this machine saved. Replies will find their way back.")
    elif saved_as is not None:
        print(
            f"`{far}` has this machine saved as `{saved_as}`, not `{here.name}`; it corrects "
            "that on its next call here."
        )
    else:
        names = ", ".join(str(e.get("name")) for e in entries or []) or "none"
        print(
            f"`{far}` did not save this machine (it has: {names}). {back} may lead somewhere "
            f"else from there. Run on {far}: meridian machine add <SSH target that reaches this "
            "machine from there>"
        )


def offer_way_back(
    target: str, path: str, far: str, far_id: str, here: MachineIdentity, *, interactive: bool
) -> None:
    entries = _far_machines(target, path, far_id)
    saved_as = _saved_there_as(entries, here)
    if saved_as is not None:
        _report(saved_as, far, here, entries, "")
        return
    reached = answering_from_there(target, back_candidates())
    suggestion = reached[0] if reached else None
    run_there = (
        f"meridian machine add {suggestion or '<SSH target that reaches this machine from there>'}"
    )
    if not interactive:
        print(f"Replies need `{far}` to save this machine too. Run there: {run_there}")
        return
    if not ask(f"Also save this machine on {far}, so its sessions can reply?", default=True):
        print(f"Replies from {far} need it. To do it later, run there: {run_there}")
        return
    if suggestion is None:
        print(f"No target I tried reaches this machine from {far}.")
    back = ask_text(f"SSH target that reaches this machine from {far}", suggestion)
    if not back or not valid_target(back):
        print(f"No target that can be sent. To do it, run on {far}: {run_there}")
        return
    status = subprocess.run(
        ["ssh", "-t", "--", target, path, "machine", "add", back], check=False
    ).returncode
    if status != 0:
        print(
            f"`machine add` on {far} stopped (exit {status}). To finish, run on {far}: "
            f"meridian machine add {shlex.quote(back)}"
        )
    after = _far_machines(target, path, far_id)
    _report(_saved_there_as(after, here), far, here, after, back)


__all__ = ["offer_way_back", "valid_target"]
