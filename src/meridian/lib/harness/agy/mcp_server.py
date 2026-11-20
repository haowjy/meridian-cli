"""``meridian harness-mcp agy``: the one-tool MCP server an ``agy`` plugin starts.

``agy`` starts one server per ``agy`` process and closes its stdin when it exits, so the
server's parent is the ``agy`` process whose conversations it will serve. Stdout is the
MCP transport: nothing here prints, and each command runs with stdio piped.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from meridian.lib.harness.agy.state import (
    process_dir,
    read_json,
    record_call,
    request_path,
)
from meridian.lib.platform.python_module import module_argv

TOOL_DESCRIPTION = (
    "Run a Meridian CLI command: args is the argument list after `meridian`. Start with "
    '["-h"] to see the commands. Commands run outside agy\'s sandbox and can start '
    "agents on other harnesses, so each call is a real action. Returns stdout, stderr "
    "and the exit code."
)
_OUTPUT_LIMIT = 256 * 1024
# MERIDIAN_HOME relocates the state the hook already wrote; the rest is identity and
# launch state that belongs to whichever session started the server.
_KEPT_ENV = frozenset({"MERIDIAN_HOME"})


def scrubbed_env(base: Mapping[str, str]) -> dict[str, str]:
    return {
        key: value
        for key, value in base.items()
        if key in _KEPT_ENV or not key.upper().startswith(("MERIDIAN_", "_MERIDIAN_"))
    }


def _clip(data: bytes) -> str:
    text = data[:_OUTPUT_LIMIT].decode(errors="replace")
    return text + "\n[output truncated]" if len(data) > _OUTPUT_LIMIT else text


def format_result(stdout: str, stderr: str, exit_code: int) -> str:
    parts = [stdout.rstrip("\n")] if stdout.strip() else []
    if stderr.strip():
        parts.append(f"[stderr]\n{stderr.rstrip(chr(10))}")
    parts.append(f"[exit code {exit_code}]")
    return "\n".join(parts)


def _request_meta(ctx: Context[Any, Any, Any]) -> dict[str, Any]:
    meta = ctx.request_context.meta
    return dict(meta.model_extra or {}) if meta is not None else {}


def _call_cwd(proc_dir: Path | None, root: str | None) -> str | None:
    """The workspace the hook saw for this conversation; the server itself starts in ``.agy/``."""

    if proc_dir is None or root is None:
        return None
    request = read_json(request_path(proc_dir, root))
    cwd = request.get("cwd") if request else None
    return cwd if isinstance(cwd, str) and Path(cwd).is_dir() else None


async def run_meridian(args: list[str], *, cwd: str | None) -> tuple[str, str, int]:
    """Run the CLI as a child of this loop: no shell, no stdin, killed if the call is cancelled."""

    process = await asyncio.create_subprocess_exec(
        *module_argv("meridian", *args),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=scrubbed_env(os.environ),
        cwd=cwd,
    )
    try:
        out, err = await process.communicate()
    except BaseException:
        process.kill()
        await asyncio.shield(process.wait())
        raise
    return _clip(out), _clip(err), process.returncode or 0


def build_server() -> FastMCP[Any]:
    server = FastMCP("meridian")
    parent_dir = process_dir(os.getppid())

    async def meridian(args: list[str], ctx: Context[Any, Any, Any]) -> str:
        meta = _request_meta(ctx)
        root = record_call(parent_dir, meta, time.time()) if parent_dir is not None else None
        stdout, stderr, code = await run_meridian(args, cwd=_call_cwd(parent_dir, root))
        return format_result(stdout, stderr, code)

    server.add_tool(meridian, name="meridian", description=TOOL_DESCRIPTION)
    return server


def run_server() -> None:
    build_server().run(transport="stdio")


__all__ = ["TOOL_DESCRIPTION", "build_server", "format_result", "run_server", "scrubbed_env"]
