"""FastMCP server entry point and operation registration."""

import time
from contextlib import asynccontextmanager
from typing import Any

from mcp.server.fastmcp import FastMCP

from meridian.lib.bootstrap.services import build_extension_entrypoint, prepare_for_runtime_read
from meridian.lib.config.project_root import resolve_project_root
from meridian.lib.core.logging import configure_logging
from meridian.lib.extensions.context import (
    ExtensionInvocationContextBuilder,
    build_extension_command_services,
)
from meridian.lib.extensions.dispatcher import ExtensionCommandDispatcher
from meridian.lib.extensions.registry import (
    build_first_party_registry,
    compute_manifest_hash,
)
from meridian.lib.extensions.types import (
    ExtensionErrorResult,
    ExtensionSurface,
)
from meridian.lib.telemetry import emit_telemetry
from meridian.lib.telemetry.bootstrap import (
    TelemetryMode,
    TelemetryPlan,
)
from meridian.lib.telemetry.bootstrap import (
    install as install_telemetry,
)


@asynccontextmanager
async def lifespan(_: FastMCP[Any]):
    """Initialize shared resources for MCP server lifetime."""

    configure_logging(json_mode=True)
    install_telemetry(TelemetryPlan(mode=TelemetryMode.STDERR, logical_owner="mcp-server"))
    yield {"ready": True}


mcp = FastMCP("meridian", lifespan=lifespan)


@mcp.tool()
async def extension_list_commands() -> dict[str, Any]:
    """List all registered extension commands.

    EB3.8: Returns same fqids as CLI discovery.
    """

    registry = build_first_party_registry()
    commands = sorted(
        registry.list_for_surface(ExtensionSurface.MCP),
        key=lambda spec: spec.fqid,
    )

    return {
        "schema_version": 1,
        "manifest_hash": compute_manifest_hash(registry)[:16],
        "commands": [
            {
                "fqid": spec.fqid,
                "extension_id": spec.extension_id,
                "command_id": spec.command_id,
                "summary": spec.summary,
                "surfaces": [surface.value for surface in sorted(spec.surfaces)],
                "requires_app_server": spec.requires_app_server,
            }
            for spec in commands
        ],
    }


@mcp.tool()
async def extension_invoke(
    fqid: str,
    args: dict[str, Any] | None = None,
    request_id: str | None = None,
    work_id: str | None = None,
    spawn_id: str | None = None,
) -> dict[str, Any]:
    """Invoke an extension command.

    EB3.9: Uses spec.extension_id/spec.command_id for invoke URL.
    EB3.10: In-process dispatch for requires_app_server=False.
    EB3.11: Returns structured error payload on failures.
    """

    start = time.monotonic()
    resolved_args = args or {}

    def emit_invoked(status: str, *, code: str | None = None) -> None:
        ids = {
            key: value
            for key, value in {
                "request_id": request_id,
                "work_id": work_id,
                "spawn_id": spawn_id,
            }.items()
            if value is not None
        }
        data: dict[str, Any] = {
            "fqid": fqid,
            "status": status,
            "latency_ms": round((time.monotonic() - start) * 1000, 1),
        }
        if code is not None:
            data["code"] = code
        emit_telemetry(
            "server",
            "mcp.command.invoked",
            scope="mcp.server",
            ids=ids or None,
            data=data,
            severity="error" if status == "error" else "info",
        )

    registry = build_first_party_registry()
    spec = registry.get(fqid)
    if spec is None:
        emit_invoked("error", code="not_found")
        return {
            "status": "error",
            "code": "not_found",
            "message": f"Command not found: {fqid}",
        }
    if ExtensionSurface.MCP not in spec.surfaces:
        emit_invoked("error", code="surface_not_allowed")
        return {
            "status": "error",
            "code": "surface_not_allowed",
            "message": f"Command {fqid} is not available via MCP",
        }

    if not spec.requires_app_server:
        # In-process dispatch for local-only commands. Rootless observability
        # stays process-scoped via stderr telemetry rather than segment storage.
        dispatcher = ExtensionCommandDispatcher(registry)
        prepared = prepare_for_runtime_read(resolve_project_root())
        application = build_extension_entrypoint(prepared)
        services = build_extension_command_services(application=application)
        context_builder = ExtensionInvocationContextBuilder(ExtensionSurface.MCP).with_authority(
            prepared.authority
        )
        if request_id is not None:
            context_builder = context_builder.with_request_id(request_id)
        if work_id is not None:
            context_builder = context_builder.with_work_id(work_id)
        if spawn_id is not None:
            context_builder = context_builder.with_spawn_id(spawn_id)
        result = await dispatcher.dispatch(
            fqid=fqid,
            args=resolved_args,
            context=context_builder.build(),
            services=services,
        )
        if isinstance(result, ExtensionErrorResult):
            emit_invoked("error", code=result.code)
            return {
                "status": "error",
                "code": result.code,
                "message": result.message,
            }
        emit_invoked("ok")
        return {"status": "ok", "result": result.payload}

    emit_invoked("error", code="app_server_archived")
    return {
        "status": "error",
        "code": "app_server_archived",
        "message": (
            "This extension requires the archived Meridian app server. "
            "Rebuild `meridian app` before invoking app-server-backed extensions."
        ),
    }

def run_server() -> None:
    """Start the FastMCP stdio server."""

    mcp.run(transport="stdio")

if __name__ == "__main__":
    run_server()
