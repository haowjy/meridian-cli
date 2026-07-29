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
from meridian.lib.ops.mars import resolve_mars_executable


@dataclass(frozen=True)
class MarsPassthroughRequest:
    command: tuple[str, ...]
    mars_args: tuple[str, ...]
    wants_json: bool
    root_override: Path | None


@dataclass(frozen=True)
class MarsPassthroughResult:
    request: MarsPassthroughRequest
    returncode: int
    stdout_text: str = ""
    stderr_text: str = ""


def mars_requested_json(args: Sequence[str]) -> bool:
    return any(token == "--json" for token in args)


def mars_requested_root(args: Sequence[str]) -> Path | None:
    index = 0
    while index < len(args):
        token = args[index]
        if token == "--root":
            next_value = args[index + 1].strip() if index + 1 < len(args) else ""
            if next_value:
                return Path(next_value)
            index += 2
            continue
        if token.startswith("--root="):
            candidate = token.partition("=")[2].strip()
            if candidate:
                return Path(candidate)
        index += 1
    return None


def decode_json_values(raw_stdout: str) -> list[object] | None:
    decoder = json.JSONDecoder()
    parsed_values: list[object] = []
    index = 0
    while index < len(raw_stdout):
        while index < len(raw_stdout) and raw_stdout[index].isspace():
            index += 1
        if index >= len(raw_stdout):
            return parsed_values
        try:
            parsed, index = decoder.raw_decode(raw_stdout, index)
        except json.JSONDecodeError:
            return None
        parsed_values.append(parsed)
    return parsed_values


def parse_mars_passthrough(
    args: Sequence[str],
    *,
    output_format: str | None = None,
    executable: str,
) -> MarsPassthroughRequest:
    """Build an executable Mars passthrough request without side effects."""

    mars_args = list(args)
    wants_json = mars_requested_json(mars_args) or output_format == "json"
    if wants_json and not mars_requested_json(mars_args):
        mars_args = ["--json", *mars_args]
    if mars_requested_root(mars_args) is None:
        project_dir = os.getenv("MERIDIAN_PROJECT_DIR", "").strip()
        if project_dir:
            mars_args = [*mars_args, "--root", project_dir]
    return MarsPassthroughRequest(
        command=(executable, *mars_args),
        mars_args=tuple(mars_args),
        wants_json=wants_json,
        root_override=mars_requested_root(mars_args),
    )


def execute_mars_passthrough(
    request: MarsPassthroughRequest,
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    stderr: TextIO | None = None,
) -> MarsPassthroughResult:
    """Execute a prepared Mars passthrough request."""

    stderr_stream = sys.stderr if stderr is None else stderr
    # With MERIDIAN_PROJECT_DIR set, parse_mars_passthrough injects --root to the
    # launcher project; Mars runs under Meridian management for all passthrough commands.
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
            return MarsPassthroughResult(
                request=request,
                returncode=result.returncode,
                stdout_text=result.stdout or "",
                stderr_text=result.stderr or "",
            )

        result = run(list(request.command), check=False, **run_kwargs)
        return MarsPassthroughResult(request=request, returncode=result.returncode)
    except FileNotFoundError:
        print(
            "error: Failed to execute 'mars'. Install meridian with dependencies and retry.",
            file=stderr_stream,
        )
        raise SystemExit(1) from None


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

    request = parse_request(
        args,
        output_format=output_format,
        executable=executable,
    )
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
