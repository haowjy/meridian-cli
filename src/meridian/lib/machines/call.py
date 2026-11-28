"""One function makes every call to another machine: ``call_machine``.

It returns one of three results and prints nothing. The split between "not sent" and
"unanswered" is the ready line: no ready line means the other side's command was never given
anything; a ready line and then anything short of a whole result line means the request may
have been acted on.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Mapping
from typing import Final

from meridian.lib.core.refs import is_machine_id, is_machine_name
from meridian.lib.machines.discover import (
    Discovery,
    discover,
    read_cached_path,
    write_cache,
)
from meridian.lib.machines.home_write import best_effort
from meridian.lib.machines.results import Answered, CallResult, NotSent, Unanswered
from meridian.lib.machines.ssh import (
    TIMED_OUT,
    SshProcess,
    SshUnavailable,
    not_connected,
)
from meridian.lib.state.saved_machines import (
    NameTaken,
    SavedMachine,
    SavedMachinesError,
    read_saved_machines,
    set_machine_name,
    update_saved_machines,
)
from meridian.lib.streaming.control_socket import MAX_REQUEST_BYTES
from meridian.lib.utils.json_shapes import json_list, json_object

logger = logging.getLogger(__name__)

CONNECT_SECONDS: Final = 15.0
"""From starting ``ssh`` to the ready line."""
MESSAGE_LIMIT_SECONDS: Final = 70
"""What the receiving ``message`` may take: 30 s to find the route and 35 s to send, plus 5."""
READ_LIMIT_SECONDS: Final = 30
"""What the receiver gives any other command."""
CALLER_GRACE_SECONDS: Final = 5.0
"""How long past ``limit`` the caller waits for the result line."""

READY_MARKER: Final = b"meridian-ready "
RESULT_MARKER: Final = b"meridian-result "
REQUEST_VERSION: Final = 1


def _ready(line: bytes) -> dict[str, object] | None:
    if not line.startswith(READY_MARKER):
        return None
    try:
        document = json.loads(line[len(READY_MARKER) :].decode("utf-8"))
    except ValueError:
        return None
    return json_object(document)


def _check_ready(ready: Mapping[str, object]) -> tuple[str, str, str, list[int]] | str:
    """The ready line's name, ID, version and request versions, or why it can't be used. It is
    data from the other machine: its name is about to be compared, printed and maybe saved."""

    name, ident, version, versions = (ready.get(key) for key in ("machine", "id", "version", "v"))
    if not isinstance(name, str) or not is_machine_name(name):
        return "its `machine` is not a machine name"
    if not isinstance(ident, str) or not is_machine_id(ident):
        return "its `id` is not 12 hex characters"
    listed = json_list(versions)
    if listed is None or not all(
        isinstance(item, int) and not isinstance(item, bool) for item in listed
    ):
        return "its `v` is not a list of integers"
    return (
        name,
        ident,
        version if isinstance(version, str) else "",
        [item for item in listed if isinstance(item, int)],
    )


class _Stale:
    """The cached path ran no Meridian: a shell on the other side answered instead."""

    def __init__(self, last_line: str) -> None:
        self.last_line = last_line


def _connect(
    target: str, label: str, path: str
) -> tuple[SshProcess, dict[str, object]] | NotSent | _Stale:
    try:
        process = SshProcess(target, (path, "machine", "run"))
    except SshUnavailable as exc:
        return NotSent("failed", str(exc))
    deadline = time.monotonic() + CONNECT_SECONDS
    while True:
        line = process.read_line(deadline)
        if line is TIMED_OUT:
            process.kill()
            return NotSent(
                "unreachable",
                f"{label} gave no ready line within {CONNECT_SECONDS:.0f} s",
            )
        if line is None:
            exited = process.finish()
            if exited.status == 255:
                return not_connected(exited, label, target)
            if exited.status < 0:
                # Our own ssh was stopped by a signal: nothing says the path is stale.
                return NotSent("unreachable", f"the ssh call to {label} was interrupted")
            return _Stale(exited.last_line or f"exited {exited.status}")
        assert isinstance(line, bytes)
        ready = _ready(line.rstrip(b"\r\n"))
        if ready is not None:
            return process, ready


def _failed(process: SshProcess, text: str, kind: str = "failed") -> NotSent:
    process.kill()
    return NotSent(kind, text)


def _correct_name(saved: SavedMachine, new_name: str, own_name: str) -> None:
    """The saved machine says it has a new name: record it. Never fails the call."""

    def record() -> None:
        outcome = update_saved_machines(
            lambda entries, raw: set_machine_name(entries, raw, saved.id, new_name, own_name)
        )
        if isinstance(outcome, NameTaken):
            logger.warning(
                "%s now calls itself `%s`, which is %s; keeping the saved name",
                saved.name,
                new_name,
                outcome.holder,
            )

    best_effort(f"record that {saved.name} is now called {new_name}", record)


def call_machine(
    target: str,
    argv: list[str],
    stdin: str | None,
    limit: int,
    expect_id: str | None = None,
    *,
    caller: Mapping[str, object] | None = None,
    binary: str | None = None,
    label: str | None = None,
) -> CallResult:
    """Run ``meridian <argv>`` on the machine at *target* through ``machine run``.

    *expect_id* is a saved machine's ID: the ready line must carry it, and a new name is
    recorded. *binary* is a path found already (``machine add``); otherwise the cached one is
    used, or discovery runs, once more if the cached path turns out stale. *label* is how
    errors name the machine: its saved name, else the target.
    """

    label = label or target

    path = binary or (read_cached_path(expect_id) if expect_id else None)
    discovered = False
    while True:
        if path is None:
            found = discover(target, label)
            if isinstance(found, NotSent):
                return found
            if found.path is None:
                return NotSent("version", missing_text(label, found))
            path, discovered = found.path, True
        connected = _connect(target, label, path)
        if isinstance(connected, _Stale):
            if discovered:
                return NotSent(
                    "failed", f"`{path} machine run` fails on {target}: {connected.last_line}"
                )
            path = None
            continue
        break
    if isinstance(connected, NotSent):
        return connected
    process, ready = connected
    try:
        return _exchange(process, ready, target, label, path, argv, stdin, limit, expect_id, caller)
    finally:
        process.reap()


def _exchange(
    process: SshProcess,
    ready: dict[str, object],
    target: str,
    label: str,
    path: str,
    argv: list[str],
    stdin: str | None,
    limit: int,
    expect_id: str | None,
    caller: Mapping[str, object] | None,
) -> CallResult:
    """From a ready line to a result: the ID check, then the request."""

    checked = _check_ready(ready)
    if isinstance(checked, str):
        return _failed(process, f"{label} sent a ready line that can't be used: {checked}")
    name, ident, version, versions = checked
    if REQUEST_VERSION not in versions:
        return _failed(
            process,
            f"{label} reads request versions {versions}, this build writes {REQUEST_VERSION}",
            "version",
        )
    if expect_id is not None and ident != expect_id:
        saved = next((entry for entry in _saved_or_empty() if entry.id == expect_id), None)
        who = f"`{saved.name}`" if saved else "the saved machine"
        return _failed(
            process,
            f"{target} now leads to another machine (`{name}`), not {who}. If {who} was "
            f"reinstalled, run `meridian machine rename {saved.name if saved else '<its name>'}` "
            f"there, then here `meridian machine remove {saved.name if saved else '<name>'}` "
            f"and `meridian machine add {target}`.",
            "refused",
        )
    if expect_id is not None:
        saved = next((entry for entry in _saved_or_empty() if entry.id == ident), None)
        if saved is not None and saved.name != name:
            from meridian.lib.machines.resolve import this_machine

            _correct_name(saved, name, this_machine().name)
        best_effort(
            "cache the path of Meridian there",
            lambda: write_cache(ident, path=path, version=version),
        )

    request = {
        "v": REQUEST_VERSION,
        "argv": argv,
        "stdin": stdin,
        "limit": limit,
        "caller": dict(caller) if caller is not None else None,
    }
    # Whatever can't be sent fails here, before a byte is written: it is not "maybe delivered".
    try:
        data = (json.dumps(request, separators=(",", ":"), ensure_ascii=False) + "\n").encode(
            "utf-8"
        )
    except UnicodeEncodeError as exc:
        return NotSent("usage", f"the text can't be sent as UTF-8: {exc.reason}")
    if len(data) > MAX_REQUEST_BYTES:
        return NotSent(
            "too_large",
            f"the request is over the {MAX_REQUEST_BYTES // (1024 * 1024)} MiB limit",
        )
    # One try from the first request byte to a parsed result: whatever goes wrong in it, the
    # command may have run, so a failure nobody listed must not read as "not sent".
    deadline = time.monotonic() + limit + CALLER_GRACE_SECONDS
    try:
        process.write(data, deadline)
        while True:
            line = process.read_line(deadline)
            if line is TIMED_OUT:
                process.kill()
                return Unanswered(f"no result within {limit + CALLER_GRACE_SECONDS:.0f} s")
            if line is None:
                exited = process.finish()
                return Unanswered(
                    f"the connection ended without a result ({exited.last_line or exited.status})"
                )
            assert isinstance(line, bytes)
            if line.startswith(RESULT_MARKER):
                result = json.loads(line[len(RESULT_MARKER) :].decode("utf-8"))
                if isinstance(result.get("refused"), str):
                    process.finish()
                    return NotSent("refused", f"{name} refused the request: {result['refused']}")
                answered = Answered(
                    exit=int(result["exit"]),
                    stdout=str(result["stdout"]),
                    stderr=str(result["stderr"]),
                    timed_out=bool(result.get("timed_out", False)),
                    truncated=bool(result.get("truncated", False)),
                    machine=name,
                    id=ident,
                    version=version,
                )
                process.finish()
                return answered
    except Exception as exc:
        process.kill()
        return Unanswered(f"{type(exc).__name__}: {exc}")


def _saved_or_empty() -> list[SavedMachine]:
    try:
        return read_saved_machines()
    except SavedMachinesError:
        return []


def missing_text(label: str, found: Discovery) -> str:
    """Why discovery on *label* found no Meridian that can take a call."""

    if found.bad_path is not None:
        return (
            f"the install path on {label} can't be used ({found.bad_path}): link Meridian into "
            "~/.local/bin"
        )
    if found.old is not None:
        path, version = found.old
        return (
            f"Meridian on {label} is too old to take calls from other machines "
            f"({version or 'unknown version'} at {path}); run `uv tool install --upgrade "
            "meridian-cli` there"
        )
    return (
        f"no Meridian on {label}: install it there with `uv tool install meridian-cli`, or if it "
        "is installed elsewhere, link it into ~/.local/bin (the search looks at "
        "`command -v meridian`, ~/.local/bin, /opt/homebrew/bin, /usr/local/bin, ~/.cargo/bin)"
    )


__all__ = [
    "CALLER_GRACE_SECONDS",
    "CONNECT_SECONDS",
    "MESSAGE_LIMIT_SECONDS",
    "READ_LIMIT_SECONDS",
    "call_machine",
    "missing_text",
]
