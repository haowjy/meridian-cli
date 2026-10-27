"""Mars CLI passthrough helpers for the meridian CLI."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from meridian import __version__
from meridian.lib.bundled_skills import WHEEL, provide_bundled_skills, report_provide_refusal
from meridian.lib.catalog.mars_roots import (
    ensure_user_mars_root,
    mars_fallback_args,
    resolve_mars_roots,
    user_mars_root,
)
from meridian.lib.config.project_root import resolve_project_root_resolution
from meridian.lib.ops.mars import resolve_mars_executable

_MUTATING_COMMANDS = frozenset(
    {
        "add",
        "remove",
        "link",
        "unlink",
        "sync",
        "upgrade",
        "rename",
        "override",
        "adopt",
        "version",
        "repair",
        "resolve",
        "migrate",
        "provide",
    }
)
_MUTATING_COMMAND_PAIRS = frozenset({("models", "alias")})
_PROVIDE_COMMANDS = frozenset(
    {"sync", "upgrade", "add", "remove", "repair", "validate", "doctor", "build"}
)
_USER_MUTATION_ERROR = (
    "no project Mars root here; run 'meridian init --link', or use 'meridian mars --user …'"
)


@dataclass(frozen=True)
class MarsPassthroughRequest:
    command: tuple[str, ...]
    mars_args: tuple[str, ...]
    wants_json: bool
    provide_root: Path | None
    user_scope: bool = False


@dataclass(frozen=True)
class MarsPassthroughResult:
    request: MarsPassthroughRequest
    returncode: int
    stdout_text: str = ""
    stderr_text: str = ""


def mars_requested_json(args: Sequence[str]) -> bool:
    return any(token == "--json" for token in args)


def _strip_meridian_mars_options(
    args: Sequence[str],
) -> tuple[list[str], bool, Path | None]:
    forwarded: list[str] = []
    user_scope = False
    requested_root: Path | None = None
    index = 0
    while index < len(args):
        token = args[index]
        if token == "--user":
            user_scope = True
            index += 1
            continue
        if token == "--root":
            if index + 1 >= len(args) or not args[index + 1].strip():
                raise ValueError("--root requires a value")
            requested_root = Path(args[index + 1]).expanduser()
            index += 2
            continue
        if token.startswith("--root="):
            raw_root = token.partition("=")[2].strip()
            if not raw_root:
                raise ValueError("--root requires a value")
            requested_root = Path(raw_root).expanduser()
            index += 1
            continue
        forwarded.append(token)
        index += 1
    return forwarded, user_scope, requested_root


def _mars_subcommand(args: Sequence[str]) -> str | None:
    return next((token for token in args if not token.startswith("-")), None)


def _is_mars_mutation(args: Sequence[str]) -> bool:
    commands = tuple(token for token in args if not token.startswith("-"))
    if commands[:1] == ("migrate",) and "--check" in args:
        return False  # reports what needs migrating and writes nothing
    return bool(
        commands and (commands[0] in _MUTATING_COMMANDS or commands[:2] in _MUTATING_COMMAND_PAIRS)
    )


def parse_mars_passthrough(
    args: Sequence[str],
    *,
    output_format: str | None = None,
    executable: str,
) -> MarsPassthroughRequest:
    """Build an executable Mars passthrough request without side effects."""

    mars_args, user_scope, requested_root = _strip_meridian_mars_options(args)
    explicit_root = requested_root.resolve() if requested_root is not None else None
    project_root = resolve_project_root_resolution(explicit_root).project_root
    roots = resolve_mars_roots(project_root)
    subcommand = _mars_subcommand(mars_args)

    # Gate before root injection so Mars's add/link auto-init can never write a
    # mars.toml into a bare project directory.
    if not user_scope and roots.scope == "user" and _is_mars_mutation(mars_args):
        raise ValueError(_USER_MUTATION_ERROR)
    if (
        user_scope
        and subcommand == "init"
        and any(token == "--link" or token.startswith("--link=") for token in mars_args)
    ):
        raise ValueError("meridian mars init --link is project-only")

    wants_json = mars_requested_json(mars_args) or output_format == "json"
    if wants_json and not mars_requested_json(mars_args):
        mars_args = ["--json", *mars_args]
    selected_root = user_mars_root() if user_scope else roots.primary
    if not user_scope and subcommand == "init":
        selected_root = project_root
    mars_args = [*mars_args, "--root", selected_root.as_posix()]
    if not user_scope and subcommand in {"agents", "skills"}:
        mars_args.extend(mars_fallback_args(roots, executable=executable))
    return MarsPassthroughRequest(
        command=(executable, *mars_args),
        mars_args=tuple(mars_args),
        wants_json=wants_json,
        provide_root=(
            selected_root
            if subcommand in _PROVIDE_COMMANDS
            and not any(argument in {"-h", "--help"} for argument in mars_args)
            else None
        ),
        user_scope=selected_root == user_mars_root(),
    )


def execute_mars_passthrough(
    request: MarsPassthroughRequest,
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    stderr: TextIO | None = None,
) -> MarsPassthroughResult:
    """Execute a prepared Mars passthrough request."""

    stderr_stream = sys.stderr if stderr is None else stderr
    if request.user_scope:
        try:
            ensure_user_mars_root(executable=request.command[0])
        except RuntimeError as exc:
            print(f"error: {exc}", file=stderr_stream)
            raise SystemExit(1) from None

    bundled_source = WHEEL
    if request.provide_root is not None:
        bundled_source = provide_bundled_skills(
            request.provide_root,
            executable=request.command[0],
            run=run,
            stderr=stderr_stream,
        )

    run_kwargs: dict[str, object] = {
        "env": {
            **os.environ,
            "MERIDIAN_MANAGED": os.environ.get("MERIDIAN_MANAGED", "1"),
            "MERIDIAN_VERSION": __version__,
        },
    }
    try:
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
    if outcome.returncode == 0:
        report_provide_refusal(bundled_source, stderr=stderr_stream)
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
