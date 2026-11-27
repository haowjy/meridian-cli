"""``forward_if_remote``: how every crossing command but ``message`` reaches another machine.

``main`` has already refused what may not cross and left ``-C`` unapplied. A crossing handler
calls this first with the references its own parser gave it. When none names another machine
it returns and the handler goes on here; otherwise it makes the call, prints what the other
side printed and exits with its status.
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Sequence
from typing import Never

from meridian.cli.invocation import GlobalOptions, get_global_options
from meridian.lib.core.refs import parse_ref
from meridian.lib.machines.call import READ_LIMIT_SECONDS, call_machine
from meridian.lib.machines.caller import is_machine_call
from meridian.lib.machines.resolve import MachineError, resolve_machine, this_machine
from meridian.lib.machines.results import Answered, NotSent, Unanswered
from meridian.lib.state.saved_machines import SavedMachine
from meridian.lib.utils.json_shapes import json_object

# An error kind is a short word: what the other side sends is not trusted to be one.
_KIND_RE = re.compile(r"[a-z_]{1,32}")


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
        print(f"error: {text}", file=sys.stderr)
    raise SystemExit(1)


def other_machine(
    refs: Sequence[str], *, option_machine: SavedMachine | None, directory_explicit: bool
) -> SavedMachine | None:
    """The one other machine this command names, or ``None`` when it all runs here.

    The one place that turns a reference into another machine, for every crossing command and
    for ``message``. Raises ``MachineError`` for: an unknown machine, two machines in one
    command, a machine beside a bare ID, ``-C`` applied here beside ``<name>/<id>``, and any
    other machine named by a command that another machine asked for (no third machine).
    """

    remote = {option_machine.id: option_machine} if option_machine is not None else {}
    bare = False
    own_named = False
    for raw in refs:
        try:
            parsed = parse_ref(raw)
        except ValueError as exc:
            raise MachineError("usage", str(exc)) from exc
        if parsed.kind != "machine":
            bare = True
            continue
        assert parsed.machine is not None
        resolved = resolve_machine(parsed.machine)
        if isinstance(resolved, SavedMachine):
            remote[resolved.id] = resolved
        else:
            own_named = True
    if not remote:
        return None
    target = next(iter(remote.values()))
    if is_machine_call():
        raise MachineError("refused", "a call from another machine can't reach a third machine")
    if len(remote) > 1 or own_named or (bare and option_machine is None):
        raise MachineError(
            "usage",
            "two machines in one command: name one machine, in `--machine` or in the references",
        )
    if directory_explicit and option_machine is None:
        raise MachineError(
            "usage",
            f"-C was applied on this machine: to name a path on {target.name}, use "
            f"`--machine {target.name} -C <path on {target.name}>` with a bare ID",
        )
    return target


def forward_if_remote(refs: Sequence[str] = ()) -> None:
    options = get_global_options()
    try:
        target = other_machine(
            refs, option_machine=options.machine, directory_explicit=options.directory_explicit
        )
        if target is None:
            return
        base = base_caller()
    except MachineError as exc:
        fail(None, exc.kind, exc.text)
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
        kind, text = _error(answer)
        fail(target.name, kind, f"{target.name}: {text}")
    sys.stdout.write(answer.stdout)
    sys.stdout.flush()
    sys.stderr.write(answer.stderr)
    raise SystemExit(answer.exit)


def _error(answer: Answered) -> tuple[str, str]:
    """The other side's error as ``(kind, text)``: from the object ``fail`` printed on its
    stdout, else from a legacy JSON object on its stderr; the ``kind`` it gave, else
    ``failed``. With neither the text is its stderr."""

    def kind_of(value: object) -> str:
        return value if isinstance(value, str) and _KIND_RE.fullmatch(value) else "failed"

    def parsed(line: str) -> dict[str, object]:
        try:
            return json_object(json.loads(line)) or {}
        except ValueError:
            return {}

    printed = parsed(answer.stdout.strip())
    error = json_object(printed.get("error")) or {}
    if printed.get("ok") is False and isinstance(error.get("text"), str):
        return kind_of(error.get("kind")), str(error["text"])
    for line in reversed(answer.stderr.strip().splitlines()):
        fields = parsed(line)
        if isinstance(fields.get("error"), str):
            return kind_of(fields.get("kind")), str(fields["error"])
    return "failed", answer.stderr.strip() or answer.stdout.strip() or f"exit status {answer.exit}"


__all__ = ["base_caller", "fail", "forward_if_remote", "other_machine", "request_argv"]
