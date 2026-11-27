"""``meridian machine run``: the receiving end of a call from another machine.

Three lines go over the SSH channel: this side prints ``meridian-ready``, reads one request
line, runs the command as a child ``meridian`` process and prints ``meridian-result``. The
command crosses only if the catalog says so. The child gets the request's ``stdin`` as its
own, because this process's stdin is the channel.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
from contextlib import suppress
from pathlib import Path
from typing import Final

from meridian import __version__
from meridian.cli.bootstrap import extract_global_options
from meridian.cli.output import normalize_output_format
from meridian.cli.startup.catalog import COMMAND_CATALOG
from meridian.cli.startup.classify import classify_invocation
from meridian.lib.machines.call import READY_MARKER, REQUEST_VERSION, RESULT_MARKER
from meridian.lib.machines.caller import MACHINE_CALL_ENV, parse_machine_caller
from meridian.lib.machines.discover import CHECK_WORD
from meridian.lib.machines.ssh import CappedReader
from meridian.lib.platform.python_module import installed_executable, module_argv, scripts_dir
from meridian.lib.state.machine_identity import read_or_create_machine_identity
from meridian.lib.state.user_paths import get_user_home
from meridian.lib.streaming.control_socket import MAX_REQUEST_BYTES
from meridian.lib.utils.json_shapes import json_list, json_object

STDOUT_CAP: Final = 8 * 1024 * 1024
STDERR_CAP: Final = 16 * 1024
_PIPE_WAIT_SECONDS: Final = 2.0
_KEPT_ENV: Final = ("HOME", "PATH", "LANG", "USER", "LOGNAME", "SHELL", "TMPDIR", "MERIDIAN_HOME")
_MAX_LIMIT_SECONDS: Final = 3600
_REQUEST_HEADROOM: Final = 64 * 1024  # argv, caller and the line break around the text


def _emit(marker: bytes, document: dict[str, object]) -> None:
    sys.stdout.buffer.write(marker + json.dumps(document, separators=(",", ":")).encode() + b"\n")
    sys.stdout.buffer.flush()


def _refusal(argv: list[str]) -> str | None:
    """Why the command may not run here, or ``None``: it must be on the catalog's list of
    commands that cross, with no global option that doesn't, and no address that doesn't."""

    try:
        cleaned, parsed = extract_global_options(
            argv,
            normalize_output_format=lambda requested, json_mode: normalize_output_format(
                requested=requested, json_mode=json_mode
            ),
        )
    except SystemExit as exc:
        return str(exc.code)
    refused = [*(["--machine"] if parsed.machine else []), *parsed.options_that_dont_cross()]
    if refused:
        return f"{', '.join(refused)} doesn't reach another machine"
    descriptor = classify_invocation(cleaned, COMMAND_CATALOG)
    if descriptor is None or not descriptor.crosses:
        command = (
            " ".join(descriptor.command_path) if descriptor is not None else " ".join(cleaned[:2])
        )
        return f"`{command}` doesn't reach another machine"
    return None


def _meridian_only_dir() -> Path:
    """A directory holding one entry, ``meridian``, a link to this build's script. Raises
    ``OSError`` when it can't be made."""

    script = installed_executable("meridian")
    if script is None:
        raise FileNotFoundError(f"no `meridian` script in {scripts_dir()}")
    link = get_user_home() / "machine" / "bin" / "meridian"
    with suppress(OSError):
        if os.readlink(link) == script:
            return link.parent
    link.parent.mkdir(parents=True, exist_ok=True)
    staged = link.with_name(f".meridian.{os.getpid()}.tmp")
    with suppress(FileNotFoundError):
        staged.unlink()
    os.symlink(script, staged)
    try:
        os.replace(staged, link)
    except OSError:
        with suppress(OSError):
            staged.unlink()
        raise
    return link.parent


def _child_env(caller: object) -> tuple[dict[str, str], str]:
    """The child's environment, and a warning for the caller when ``PATH`` had to fall back."""

    env = {
        key: value for key, value in os.environ.items() if key in _KEPT_ENV or key.startswith("LC_")
    }
    # An SSH command's PATH is the system's, without the directory Meridian is installed in,
    # and what the command starts (an `inbox.received` hook row) runs `meridian` by name. The
    # install's own script directory also holds `python3`, which must not shadow the system's.
    warning = ""
    try:
        first = _meridian_only_dir()
    except OSError as exc:
        first = scripts_dir()
        warning = (
            f"warning: can't link `meridian` under the Meridian home ({exc}); PATH for this "
            f"command starts with {first}, so its `python3` comes first too\n"
        )
    env["PATH"] = os.pathsep.join(filter(None, [str(first), env.get("PATH")]))
    env[MACHINE_CALL_ENV] = json.dumps(caller if isinstance(caller, dict) else {})
    return env, warning


def _run_child(argv: list[str], stdin: str | None, limit: int, caller: object) -> dict[str, object]:
    env, warning = _child_env(caller)
    child = subprocess.Popen(
        module_argv("meridian", *argv),
        stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        # Its own process group, so stopping it stops what it started too: the group of the
        # child this process started, never a process it didn't.
        start_new_session=True,
    )
    assert child.stdout is not None and child.stderr is not None
    out, err = CappedReader(child.stdout, STDOUT_CAP), CappedReader(child.stderr, STDERR_CAP)
    if stdin is not None:
        threading.Thread(target=_feed, args=(child, stdin), daemon=True).start()
    threading.Thread(target=_stop_when_caller_leaves, args=(child,), daemon=True).start()
    timed_out = False
    try:
        child.wait(timeout=limit)
    except subprocess.TimeoutExpired:
        # Only the process this one started, through its handle: nothing recorded, no PID guessed.
        timed_out = True
        _stop(child)
        child.wait()
    return {
        "exit": child.returncode,
        "stdout": out.collect(_PIPE_WAIT_SECONDS).decode("utf-8", "replace"),
        "stderr": warning + err.collect(_PIPE_WAIT_SECONDS).decode("utf-8", "replace"),
        "timed_out": timed_out,
        "truncated": out.truncated,
    }


def _stop(child: subprocess.Popen[bytes]) -> None:
    """Kill the child's process group while the child is unreaped, so its pid is still ours."""

    if child.poll() is None:
        with suppress(ProcessLookupError, PermissionError):
            os.killpg(child.pid, signal.SIGKILL)


def _feed(child: subprocess.Popen[bytes], text: str) -> None:
    assert child.stdin is not None
    try:
        child.stdin.write(text.encode("utf-8"))
        child.stdin.close()
    except OSError:
        pass


def _stop_when_caller_leaves(child: subprocess.Popen[bytes]) -> None:
    """Without a terminal, ending the caller's ``ssh`` leaves this process running: EOF on the
    channel is the only sign the caller is gone."""

    while os.read(0, 65536):
        pass
    _stop(child)


def run_machine_call(*, check: bool) -> None:
    if check:
        print(CHECK_WORD)
        return
    identity = read_or_create_machine_identity()
    sys.stdout.buffer.write(b"\n")
    _emit(
        READY_MARKER,
        {
            "machine": identity.name,
            "id": identity.id,
            "version": __version__,
            "v": [REQUEST_VERSION],
        },
    )
    line = sys.stdin.buffer.readline(MAX_REQUEST_BYTES + _REQUEST_HEADROOM)
    try:
        request = json_object(json.loads(line.decode("utf-8")))
    except ValueError:
        request = None
    parsed = _parse_request(request) if request is not None else None
    if request is None or parsed is None:
        too_big = len(line) >= MAX_REQUEST_BYTES + _REQUEST_HEADROOM and not line.endswith(b"\n")
        _emit(
            RESULT_MARKER,
            _refused(
                "the request is over the size limit"
                if too_big
                else "the request is not a version-1 request"
            ),
        )
        return
    argv, stdin, limit = parsed
    refusal = _refusal(argv)
    if refusal is not None:
        _emit(RESULT_MARKER, _refused(refusal))
        return
    caller = request.get("caller")
    parsed_caller = parse_machine_caller(caller)
    if parsed_caller is None:
        _emit(RESULT_MARKER, _refused("the request names no usable caller"))
        return
    if parsed_caller.machine_id == identity.id:
        # Two machines with one identity file (a cloned home, a restored backup): the caller's
        # sessions would pass for this machine's own.
        _emit(
            RESULT_MARKER,
            _refused(
                "the calling machine has this machine's ID: one of the two is a copy. Remove "
                "`machine.json` from the copy's Meridian home so it gets its own ID"
            ),
        )
        return
    _emit(RESULT_MARKER, _run_child(argv, stdin, limit, caller))


def _parse_request(request: dict[str, object]) -> tuple[list[str], str | None, int] | None:
    argv, stdin, limit = request.get("argv"), request.get("stdin"), request.get("limit")
    arguments = json_list(argv)
    if (
        request.get("v") != REQUEST_VERSION
        or arguments is None
        or not arguments
        or not all(isinstance(item, str) for item in arguments)
        or not (stdin is None or isinstance(stdin, str))
        or not isinstance(limit, int)
        or isinstance(limit, bool)
        or not 0 < limit <= _MAX_LIMIT_SECONDS
    ):
        return None
    return [str(item) for item in arguments], stdin, limit


def _refused(text: str) -> dict[str, object]:
    return {"exit": 2, "stdout": "", "stderr": "", "refused": text}


__all__ = ["run_machine_call"]
