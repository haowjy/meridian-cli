"""``meridian harness-mcp agy``: the one-tool MCP server an ``agy`` plugin starts.

``agy`` starts one server per ``agy`` process and closes its stdin when it exits, so the
server's parent is the ``agy`` process whose conversations it will serve. The server holds
a Meridian row for each (``holds.py``) and runs each tool call as its conversation's row.
Stdout is the MCP transport: nothing here prints, and each command runs with stdio piped.
"""

from __future__ import annotations

import asyncio
import os
import signal
import threading
from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from meridian.lib.core.logging import configure_logging
from meridian.lib.harness.agy.holds import ConversationHolds, HeldRow
from meridian.lib.harness.agy.notices import send_owed_notices
from meridian.lib.harness.agy.state import (
    CONVERSATION_ID_KEY,
    ROOT_CONVERSATION_ID_KEY,
    is_safe_id,
    process_dir,
    scrubbed_env,
)
from meridian.lib.platform.python_module import module_argv

TOOL_DESCRIPTION = (
    "Run a Meridian CLI command: args is the argument list after `meridian`. Start with "
    '["-h"] to see the commands. Commands run outside agy\'s sandbox and can start '
    "agents on other harnesses, so each call is a real action. Returns stdout, stderr "
    "and the exit code."
)
_OUTPUT_LIMIT = 256 * 1024
_ADOPT_POLL_SECONDS = 1.0


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


def root_conversation(meta: Mapping[str, Any]) -> str | None:
    """The root conversation of a tool call: a native subagent's call names its parent's."""

    root = meta.get(ROOT_CONVERSATION_ID_KEY) or meta.get(CONVERSATION_ID_KEY)
    return root if is_safe_id(root) else None


def call_env(row: HeldRow | None) -> dict[str, str]:
    """The environment of one command: the session's identity, or none when it has no row.

    The server's own environment belongs to whoever started ``agy``, so identity comes
    only from the conversation's row, call by call.
    """

    env = scrubbed_env(os.environ)
    if row is not None:
        from meridian.lib.core.resolved_context import ResolvedContext
        from meridian.lib.core.types import SpawnId

        env.update(
            ResolvedContext(
                spawn_id=SpawnId(row.spawn_id),
                project_root=row.project_root,
                runtime_root=row.runtime_root,
                chat_id=row.chat_id,
            ).child_env_overrides(increment_depth=False)
        )
    return env


async def run_meridian(
    args: list[str], *, env: Mapping[str, str], cwd: Path | None
) -> tuple[str, str, int]:
    """Run the CLI as a child of this loop: no shell, no stdin, killed if the call is cancelled."""

    process = await asyncio.create_subprocess_exec(
        *module_argv("meridian", *args),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=dict(env),
        cwd=cwd,
    )
    try:
        out, err = await process.communicate()
    except BaseException:
        process.kill()
        await asyncio.shield(process.wait())
        raise
    return _clip(out), _clip(err), process.returncode or 0


def build_server(holds: ConversationHolds | None) -> FastMCP[Any]:
    @asynccontextmanager
    async def lifespan(_: FastMCP[Any]) -> AsyncGenerator[None]:
        async def adopt_new() -> None:
            while holds is not None:
                await asyncio.to_thread(holds.sync)
                await asyncio.sleep(_ADOPT_POLL_SECONDS)

        poller = asyncio.create_task(adopt_new())
        try:
            yield
        finally:
            poller.cancel()

    server = FastMCP("meridian", lifespan=lifespan)

    async def meridian(args: list[str], ctx: Context[Any, Any, Any]) -> str:
        root = root_conversation(_request_meta(ctx))
        row = (
            await asyncio.to_thread(holds.row_for, root)
            if holds is not None and root is not None
            else None
        )
        stdout, stderr, code = await run_meridian(
            args, env=call_env(row), cwd=row.cwd if row is not None else None
        )
        return format_result(stdout, stderr, code)

    server.add_tool(meridian, name="meridian", description=TOOL_DESCRIPTION)
    return server


def run_server() -> None:
    """Serve until ``agy`` closes stdin; then end this process's rows before SIGKILL arrives."""

    # structlog prints to stdout by default, which is the MCP transport.
    configure_logging()
    proc_dir = process_dir(os.getppid())
    holds = ConversationHolds(proc_dir) if proc_dir is not None else None
    # Notices can block for as long as a message send; do not hold up the first tool call.
    threading.Thread(target=send_owed_notices, name="meridian-agy-notices", daemon=True).start()

    def on_sigterm(signum: int, frame: object) -> None:
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, on_sigterm)
    try:
        build_server(holds).run(transport="stdio")
    finally:
        if holds is not None:
            holds.close()


__all__ = ["TOOL_DESCRIPTION", "build_server", "call_env", "format_result", "run_server"]
