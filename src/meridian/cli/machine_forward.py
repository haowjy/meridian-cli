"""``forward_if_remote``: how every crossing command but ``message`` reaches another machine.

``main`` has already refused what may not cross and left ``-C`` unapplied. A crossing handler
calls this first with the references its own parser gave it. When none names another machine
it returns and the handler goes on here; otherwise it makes the call, prints what the other
side printed and exits with its status.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Sequence
from typing import Never

from meridian.cli.invocation import GlobalOptions, get_global_options
from meridian.lib.core.refs import parse_ref
from meridian.lib.machines.call import READ_LIMIT_SECONDS, call_machine
from meridian.lib.machines.resolve import MachineError, resolve_machine, this_machine
from meridian.lib.machines.results import Answered, NotSent, Unanswered
from meridian.lib.state.saved_machines import SavedMachine
from meridian.lib.utils.json_shapes import json_object


def base_caller() -> dict[str, object]:
    """The part of a request's ``caller`` every call carries."""

    here = this_machine()
    return {"machine": here.name, "machine_id": here.id}


def request_argv(options: GlobalOptions) -> list[str]:
    """The command as sent: as typed without global options, plus the ones that cross."""

    argv = list(options.argv)
    if options.remote_directory is not None:
        argv += ["-C", options.remote_directory]
    return [*argv, "--format", options.output.format]


def fail(machine: str | None, kind: str, text: str) -> Never:
    """Print an error as a call-through command does and exit 1."""

    if get_global_options().output.format == "json":
        payload: dict[str, object] = {"ok": False}
        if machine is not None:
            payload["machine"] = machine
        payload["error"] = {"kind": kind, "text": text}
        print(json.dumps(payload, separators=(",", ":"), sort_keys=True))
    else:
        print(f"Error: {text}", file=sys.stderr)
    raise SystemExit(1)


def _target(options: GlobalOptions, refs: Sequence[str]) -> SavedMachine | None:
    """The one other machine this command names, or ``None`` when it all runs here."""

    remote = {options.machine.id: options.machine} if options.machine is not None else {}
    bare = False
    own_named = False
    for raw in refs:
        parsed = parse_ref(raw)
        if parsed.kind != "machine":
            bare = True
            continue
        assert parsed.machine is not None
        try:
            resolved = resolve_machine(parsed.machine)
        except MachineError as exc:
            fail(None, exc.kind, exc.text)
        if isinstance(resolved, SavedMachine):
            remote[resolved.id] = resolved
        else:
            own_named = True
    if not remote:
        return None
    if len(remote) > 1 or own_named or (bare and options.machine is None):
        fail(
            None,
            "usage",
            "two machines in one command: name one machine, in `--machine` or in the references",
        )
    target = next(iter(remote.values()))
    if options.directory_explicit and options.machine is None:
        fail(
            target.name,
            "usage",
            f"-C was applied on this machine: to name a path on {target.name}, use "
            f"`--machine {target.name} -C <path on {target.name}>` with a bare ID",
        )
    return target


def forward_if_remote(refs: Sequence[str] = ()) -> None:
    options = get_global_options()
    target = _target(options, refs)
    if target is None:
        return
    try:
        base = base_caller()
    except MachineError as exc:
        fail(target.name, exc.kind, exc.text)
    result = call_machine(
        target.ssh,
        request_argv(options),
        None,
        READ_LIMIT_SECONDS,
        target.id,
        caller=base,
        label=target.name,
    )
    match result:
        case NotSent(kind, text):
            fail(target.name, kind, text)
        case Unanswered(text):
            # A read is safe to run again.
            fail(target.name, "unreachable", f"{target.name} did not answer: {text}")
        case Answered():
            _finish(target, result)


def _finish(target: SavedMachine, answer: Answered) -> Never:
    if answer.truncated:
        fail(
            target.name,
            "too_large",
            "the answer is over the size limit: narrow the read (`--tail`, `--limit`)",
        )
    if answer.timed_out:
        fail(
            target.name,
            "unreachable",
            f"timed out after {READ_LIMIT_SECONDS} s on {target.name}",
        )
    if answer.exit != 0 and get_global_options().output.format == "json":
        fail(target.name, "failed", _error_text(answer))
    sys.stdout.write(answer.stdout)
    sys.stdout.flush()
    sys.stderr.write(answer.stderr)
    raise SystemExit(answer.exit)


def _error_text(answer: Answered) -> str:
    """The other side's error: the ``error`` of its legacy JSON object, else its stderr."""

    for line in reversed(answer.stderr.strip().splitlines()):
        try:
            document = json.loads(line)
        except ValueError:
            continue
        fields = json_object(document)
        if fields is not None and isinstance(fields.get("error"), str):
            return str(fields["error"])
    return answer.stderr.strip() or answer.stdout.strip() or f"exit status {answer.exit}"


__all__ = ["base_caller", "fail", "forward_if_remote", "request_argv"]
