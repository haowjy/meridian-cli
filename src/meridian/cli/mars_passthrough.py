"""Mars CLI passthrough helpers for the meridian CLI."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from meridian import __version__
from meridian.lib.catalog.mars_cli import (
    MARS_UPGRADE_HINT,
    mars_content_args,
    parse_mars_root,
    provided_sync_args,
    resolve_mars_executable,
)
from meridian.lib.config.project_root import resolve_project_root_resolution

# Mars creates a project for these when none encloses the directory; Meridian refuses instead.
# Every other write with no project fails in Mars, with its own message.
_AUTO_INIT_COMMANDS = frozenset({"add", "link"})
_USER_MUTATION_ERROR = (
    "no project Mars root here; run 'meridian init --link', or use 'meridian mars --user …'"
)


@dataclass(frozen=True)
class MarsPassthroughRequest:
    command: tuple[str, ...]
    mars_args: tuple[str, ...]
    wants_json: bool
    # Where Mars runs: it chooses the root from there, as it would for the user.
    cwd: Path
    user_scope: bool = False
    # Root-selection flags the user typed (``--root X``), forwarded to the project probe.
    root_args: tuple[str, ...] = ()
    needs_project: bool = False


@dataclass(frozen=True)
class MarsPassthroughResult:
    request: MarsPassthroughRequest
    returncode: int
    stdout_text: str = ""
    stderr_text: str = ""


def mars_requested_json(args: Sequence[str]) -> bool:
    return any(token == "--json" for token in args)


def _root_args(args: Sequence[str]) -> tuple[str, ...]:
    for index, token in enumerate(args):
        if token == "--root":
            if index + 1 >= len(args) or not args[index + 1].strip():
                raise ValueError("--root requires a value")
            return (token, args[index + 1])
        if token.startswith("--root="):
            if not token.partition("=")[2].strip():
                raise ValueError("--root requires a value")
            return (token,)
    return ()


def _mars_subcommand(args: Sequence[str]) -> str | None:
    root_value = _root_args(args)[1:]
    return next(
        (token for token in args if not token.startswith("-") and (token,) != root_value), None
    )


def parse_mars_passthrough(
    args: Sequence[str],
    *,
    output_format: str | None = None,
    executable: str,
) -> MarsPassthroughRequest:
    """Build an executable Mars passthrough request without side effects.

    ``--user`` and ``--root`` are Mars's own flags and are forwarded as typed; otherwise Mars
    chooses the root from the project directory.
    """

    mars_args = list(args)
    user_scope = "--user" in mars_args
    root_args = _root_args(mars_args)
    subcommand = _mars_subcommand(mars_args)
    wants_help = any(argument in {"-h", "--help"} for argument in mars_args)

    wants_json = mars_requested_json(mars_args) or output_format == "json"
    if wants_json and not mars_requested_json(mars_args):
        mars_args = ["--json", *mars_args]
    if not user_scope and not wants_help:
        if subcommand in {"agents", "skills"}:
            mars_args.extend(mars_content_args())
        if subcommand == "sync" and not any(
            token == "--provided-root" or token.startswith("--provided-root=")
            for token in mars_args
        ):
            mars_args.extend(provided_sync_args())
    return MarsPassthroughRequest(
        command=(executable, *mars_args),
        mars_args=tuple(mars_args),
        wants_json=wants_json,
        cwd=resolve_project_root_resolution(None).project_root,
        user_scope=user_scope,
        root_args=root_args,
        needs_project=not user_scope and not wants_help and subcommand in _AUTO_INIT_COMMANDS,
    )


def _refusal(
    request: MarsPassthroughRequest,
    *,
    run: Callable[..., subprocess.CompletedProcess[str]],
) -> str | None:
    """Why Meridian will not forward the request, or None to forward it."""

    if request.user_scope:
        # An older Mars rejects the flag with clap's usage error; say what to upgrade instead.
        probe = run(
            [request.command[0], "--user", "--help"],
            cwd=request.cwd,
            check=False,
            capture_output=True,
            text=True,
        )
        if probe.returncode != 0:
            return f"the installed Mars does not accept --user. {MARS_UPGRADE_HINT}"
    if request.needs_project:
        probe = run(
            [request.command[0], "agents", "list", "--json", *request.root_args],
            cwd=request.cwd,
            check=False,
            capture_output=True,
            text=True,
        )
        try:
            root = parse_mars_root(json.loads(probe.stdout))
        except ValueError:
            root = None
        if root is not None and root.kind == "user":
            return _USER_MUTATION_ERROR
    return None


def execute_mars_passthrough(
    request: MarsPassthroughRequest,
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    stderr: TextIO | None = None,
) -> MarsPassthroughResult:
    """Execute a prepared Mars passthrough request."""

    stderr_stream = sys.stderr if stderr is None else stderr
    run_kwargs: dict[str, object] = {
        "cwd": request.cwd,
        "env": {
            **os.environ,
            "MERIDIAN_MANAGED": os.environ.get("MERIDIAN_MANAGED", "1"),
            "MERIDIAN_VERSION": __version__,
        },
    }
    try:
        refusal = _refusal(request, run=run)
        if refusal is not None:
            print(f"error: {refusal}", file=stderr_stream)
            raise SystemExit(1)
        if request.wants_json:
            result = run(
                list(request.command),
                check=False,
                capture_output=True,
                text=True,
                **run_kwargs,
            )
            outcome = MarsPassthroughResult(
                request=request,
                returncode=result.returncode,
                stdout_text=result.stdout or "",
                stderr_text=result.stderr or "",
            )
        else:
            result = run(list(request.command), check=False, **run_kwargs)
            outcome = MarsPassthroughResult(request=request, returncode=result.returncode)
    except FileNotFoundError:
        print(
            "error: Failed to execute 'mars'. Install meridian with dependencies and retry.",
            file=stderr_stream,
        )
        raise SystemExit(1) from None
    return outcome


def run_mars_passthrough(
    args: Sequence[str],
    *,
    output_format: str | None = None,
    resolve_executable: Callable[[], str | None] = resolve_mars_executable,
    parse_request: Callable[..., MarsPassthroughRequest] = parse_mars_passthrough,
    execute_request: Callable[[MarsPassthroughRequest], MarsPassthroughResult] = (
        execute_mars_passthrough
    ),
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> None:
    stdout_stream = sys.stdout if stdout is None else stdout
    stderr_stream = sys.stderr if stderr is None else stderr
    executable = resolve_executable()
    if executable is None:
        print(
            "error: Failed to execute 'mars'. Install meridian with dependencies and retry.",
            file=stderr_stream,
        )
        raise SystemExit(1)

    try:
        request = parse_request(
            args,
            output_format=output_format,
            executable=executable,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=stderr_stream)
        raise SystemExit(1) from None
    result = execute_request(request)
    if request.wants_json:
        if result.stdout_text:
            stdout_stream.write(result.stdout_text)
        if result.stderr_text:
            stderr_stream.write(result.stderr_text)
    raise SystemExit(result.returncode)


def resolve_init_project_root(path: str | None) -> Path:
    if path:
        return Path(path).expanduser().resolve()
    env_root = os.getenv("MERIDIAN_PROJECT_DIR", "").strip()
    return Path(env_root).expanduser().resolve() if env_root else Path.cwd().resolve()
