"""CLI command for the local headless chat backend."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, cast
from urllib.parse import urlparse
from uuid import uuid4

from cyclopts import App, Parameter

from meridian.cli.utils import (
    parse_csv_list,
    require_established_project_root,
)
from meridian.lib.bootstrap.services import (
    build_chat_entrypoint,
    prepare_for_runtime_read,
    prepare_for_runtime_write,
)
from meridian.lib.catalog.catalog_session import CatalogSession
from meridian.lib.chat.backend_acquisition import ColdSpawnAcquisition
from meridian.lib.chat.frontend import FrontendAssets, resolve_frontend_assets
from meridian.lib.chat.normalization.base import EventNormalizer
from meridian.lib.chat.normalization.registry import get_normalizer_factory
from meridian.lib.chat.policy import (
    ChatBackendLaunchPlan,
    ChatPolicySnapshot,
    build_chat_backend_launch_plan,
    snapshot_from_resolved_policy,
)
from meridian.lib.chat.runtime import PipelineLookup, build_chat_runtime_from_entrypoint
from meridian.lib.config.settings import load_config
from meridian.lib.core.overrides import RuntimeOverrides
from meridian.lib.core.types import HarnessId, SpawnId
from meridian.lib.harness.registry import HarnessRegistry, get_default_harness_registry
from meridian.lib.launch.launch_types import CompositionWarning
from meridian.lib.launch.policies import SurfacePolicyInput, resolve_launch_policy
from meridian.lib.launch.request import LaunchCompositionSurface
from meridian.lib.service_context import ChatEntryPoint
from meridian.lib.state.user_paths import get_user_home
from meridian.lib.streaming.spawn_manager import SpawnManager

CHAT_SERVER_FILE = "chat-server.json"


def register_chat_command(app: App) -> None:
    """Register the ``meridian chat`` command and management subcommands."""

    chat_app = App(
        name="chat",
        help="Start or manage the local chat backend.",
        help_formatter="plain",
    )
    app.command(chat_app, name="chat")
    chat_app.default(_chat)
    chat_app.command(name="ls")(_chat_ls)
    chat_app.command(name="show")(_chat_show)
    chat_app.command(name="log")(_chat_log)
    chat_app.command(name="close")(_chat_close)


def _chat(
    model: Annotated[
        str | None,
        Parameter(name=["--model", "-m"], help="Model id or alias for chat backends."),
    ] = None,
    harness: Annotated[
        str | None,
        Parameter(name="--harness", help="Harness id: claude, codex, cursor, or opencode."),
    ] = None,
    agent: Annotated[
        str | None,
        Parameter(name=["--agent", "-a"], help="Agent profile name."),
    ] = None,
    skills: Annotated[
        tuple[str, ...],
        Parameter(
            name=["--skills", "-s"],
            help="Comma-separated ad-hoc skills to load. Repeatable.",
            negative_iterable=(),
        ),
    ] = (),
    approval: Annotated[
        str | None,
        Parameter(name="--approval", help="Approval mode: default, confirm, auto, never."),
    ] = None,
    sandbox: Annotated[
        str | None,
        Parameter(name="--sandbox", help="Sandbox mode override."),
    ] = None,
    effort: Annotated[
        str | None,
        Parameter(name="--effort", help="Effort override: low, medium, high, xhigh, max."),
    ] = None,
    autocompact: Annotated[
        int | None,
        Parameter(name="--autocompact", help="Autocompact token threshold (minimum 1000)."),
    ] = None,
    autocompact_pct: Annotated[
        int | None,
        Parameter(
            name="--autocompact-pct",
            help="Percentage of context window for autocompact (1-100).",
        ),
    ] = None,
    yolo: Annotated[
        bool,
        Parameter(name="--yolo", help="Shorthand for --approval never."),
    ] = False,
    port: Annotated[int, Parameter(name="--port", help="Port to bind; 0 auto-assigns.")] = 0,
    host: Annotated[
        str,
        Parameter(name="--host", help="Host/interface to bind."),
    ] = "127.0.0.1",
    headless: Annotated[
        bool,
        Parameter(name="--headless", help="Run API-only backend without frontend UI."),
    ] = False,
    dev: Annotated[
        bool,
        Parameter(name="--dev", help="Dev mode: Vite subprocess, verbose logging."),
    ] = False,
    frontend_root: Annotated[
        str | None,
        Parameter(name="--frontend-root", help="Path to meridian-web source checkout (dev mode)."),
    ] = None,
    frontend_dist: Annotated[
        str | None,
        Parameter(name="--frontend-dist", help="Path to built frontend assets directory."),
    ] = None,
    open_browser: Annotated[
        bool,
        Parameter(name="--open", help="Open browser after server starts."),
    ] = False,
    tailscale: Annotated[
        bool,
        Parameter(
            name="--tailscale",
            help="Share dev server on Tailscale network (requires portless).",
        ),
    ] = False,
    no_portless: Annotated[
        bool,
        Parameter(name="--no-portless", help="Disable portless in dev mode; use raw Vite."),
    ] = False,
    funnel: Annotated[
        bool,
        Parameter(name="--funnel", help="Expose the dev UI publicly via Tailscale Funnel."),
    ] = False,
    portless_force: Annotated[
        bool,
        Parameter(name="--portless-force", help="Take over an occupied portless dev route."),
    ] = False,
) -> None:
    """Start the local chat backend server."""

    from meridian.cli.main import get_global_options

    if not dev and not headless and os.environ.get("MERIDIAN_ENV") == "dev":
        dev = True

    effective_harness = harness or get_global_options().harness
    if yolo and approval is not None:
        raise ValueError(
            "Cannot use --yolo with --approval (--yolo is shorthand for --approval never)."
        )
    resolved_approval = approval if approval is not None else ("never" if yolo else None)
    parsed_skills: list[str] = []
    for token in skills:
        parsed_skills.extend(parse_csv_list(token, field_name="skills"))
    run_chat_server(
        model=model,
        harness=effective_harness,
        agent=agent,
        skills=tuple(parsed_skills),
        approval=resolved_approval,
        sandbox=sandbox,
        effort=effort,
        autocompact=autocompact,
        autocompact_pct=autocompact_pct,
        port=port,
        host=host,
        headless=headless,
        dev=dev,
        frontend_root=frontend_root,
        frontend_dist=frontend_dist,
        open_browser=open_browser,
        tailscale=tailscale,
        no_portless=no_portless,
        funnel=funnel,
        portless_force=portless_force,
    )


def _chat_ls(
    url: Annotated[str | None, Parameter(name="--url", help="Chat server base URL.")] = None,
) -> None:
    entrypoint = _prepare_chat_runtime_read_entrypoint()
    response = _request_json("GET", "/chat", url=url, runtime_root=entrypoint.context.runtime_root)
    rows = response.get("chats", [])
    if not isinstance(rows, list):
        raise ValueError("invalid chat server response: chats must be a list")
    print(_format_chat_table(cast("list[dict[str, object]]", rows)))


def _chat_show(
    chat_id: Annotated[str, Parameter(help="Chat id to inspect.")],
    url: Annotated[str | None, Parameter(name="--url", help="Chat server base URL.")] = None,
) -> None:
    entrypoint = _prepare_chat_runtime_read_entrypoint()
    runtime_root = entrypoint.context.runtime_root
    state = _request_json("GET", f"/chat/{chat_id}/state", url=url, runtime_root=runtime_root)
    events = _request_json(
        "GET",
        f"/chat/{chat_id}/events?last=5",
        url=url,
        runtime_root=runtime_root,
    )
    print(f"chat_id: {state.get('chat_id', chat_id)}")
    print(f"state: {state.get('state', 'unknown')}")
    print("events:")
    for event in _events_from_response(events):
        print(f"  {_format_event_summary(event)}")


def _chat_log(
    chat_id: Annotated[str, Parameter(help="Chat id to read.")],
    url: Annotated[str | None, Parameter(name="--url", help="Chat server base URL.")] = None,
    last: Annotated[
        int | None,
        Parameter(name="--last", help="Show only the last N events."),
    ] = None,
    follow: Annotated[
        bool,
        Parameter(name="--follow", help="Follow live events over WebSocket."),
    ] = False,
) -> None:
    entrypoint = _prepare_chat_runtime_read_entrypoint()
    runtime_root = entrypoint.context.runtime_root
    query = f"?last={last}" if last is not None else ""
    response = _request_json(
        "GET",
        f"/chat/{chat_id}/events{query}",
        url=url,
        runtime_root=runtime_root,
    )
    events = _events_from_response(response)
    for event in events:
        print(json.dumps(event, sort_keys=True))
    if follow:
        last_seq = _last_seq(events)
        asyncio.run(
            _follow_chat_log(chat_id, url=url, last_seq=last_seq, runtime_root=runtime_root)
        )


def _chat_close(
    chat_id: Annotated[str, Parameter(help="Chat id to close.")],
    url: Annotated[str | None, Parameter(name="--url", help="Chat server base URL.")] = None,
) -> None:
    entrypoint = _prepare_chat_runtime_read_entrypoint()
    response = _request_json(
        "POST",
        f"/chat/{chat_id}/close",
        url=url,
        runtime_root=entrypoint.context.runtime_root,
    )
    status = response.get("status", "unknown")
    if status != "accepted":
        error = response.get("error", "unknown")
        raise ValueError(f"chat close rejected: {error}")
    print(f"closed {chat_id}")


def run_chat_server(
    *,
    model: str | None = None,
    harness: str | None = None,
    agent: str | None = None,
    skills: tuple[str, ...] = (),
    approval: str | None = None,
    sandbox: str | None = None,
    effort: str | None = None,
    autocompact: int | None = None,
    autocompact_pct: int | None = None,
    port: int = 0,
    host: str = "127.0.0.1",
    headless: bool = False,
    dev: bool = False,
    frontend_root: str | None = None,
    frontend_dist: str | None = None,
    open_browser: bool = False,
    tailscale: bool = False,
    no_portless: bool = False,
    funnel: bool = False,
    portless_force: bool = False,
    entrypoint: ChatEntryPoint | None = None,
    uvicorn_run: Callable[..., Any] | None = None,
    stdout: Any | None = None,
) -> int:
    """Configure and run the local chat backend; return the bound port."""

    import uvicorn

    from meridian.lib.chat.server import app as chat_app
    from meridian.lib.chat.server import configure

    if port < 0 or port > 65535:
        raise ValueError("port must be between 0 and 65535")

    output = stdout if stdout is not None else sys.stdout

    if open_browser and headless:
        print("Warning: --open is ignored in headless mode.", file=output, flush=True)
        open_browser = False

    if dev and frontend_dist is not None:
        print("Error: --frontend-dist cannot be combined with --dev.", file=output, flush=True)
        sys.exit(1)
    if not dev and frontend_root is not None:
        print("Error: --frontend-root is only valid with --dev.", file=output, flush=True)
        sys.exit(1)
    if not dev and no_portless:
        print("Error: --no-portless is only valid with --dev.", file=output, flush=True)
        sys.exit(1)
    if not dev and (tailscale or funnel):
        print("Error: --tailscale and --funnel are only valid with --dev.", file=output, flush=True)
        sys.exit(1)
    if headless and dev:
        print("Error: --dev cannot be combined with --headless.", file=output, flush=True)
        sys.exit(1)
    if headless and (no_portless or tailscale or funnel or portless_force):
        print(
            "Error: dev frontend flags cannot be combined with --headless.", file=output, flush=True
        )
        sys.exit(1)
    if not dev and portless_force:
        print(
            "Error: --portless-force is only valid with portless dev mode.", file=output, flush=True
        )
        sys.exit(1)

    project_root = _resolve_chat_project_root(entrypoint)
    policy_snapshot = _resolve_chat_policy_snapshot(
        project_root=project_root,
        model=model,
        harness=harness,
        agent=agent,
        skills=skills,
        approval=approval,
        sandbox=sandbox,
        effort=effort,
        autocompact=autocompact,
        autocompact_pct=autocompact_pct,
    )
    _emit_chat_policy_warnings(policy_snapshot.warnings, output)
    if entrypoint is None:
        prepared = prepare_for_runtime_write(project_root)
        entrypoint = build_chat_entrypoint(prepared)
    project_root = _chat_project_root(entrypoint)
    runtime = build_chat_runtime_from_entrypoint(
        entrypoint=entrypoint,
        default_policy_snapshot=policy_snapshot,
        acquisition_factory=_ChatBackendAcquisitionFactory(
            policy_snapshot=policy_snapshot,
        ),
    )
    discovery_runtime_root = entrypoint.context.runtime_root
    configure(runtime=runtime)
    env_port = int(os.environ.get("PORT", "0") or "0")
    actual_port = port if port != 0 else (env_port or _find_free_port(host))
    display_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    url = f"http://{display_host}:{actual_port}"

    if dev:
        from meridian.lib.chat.dev_frontend import (
            DevFrontendConfigurationError,
            DevSupervisor,
            resolve_dev_frontend_launcher,
            resolve_dev_frontend_root,
            validate_dev_prerequisites,
        )
        from meridian.lib.chat.dev_frontend.launcher import FrontendLaunchError

        try:
            launcher = resolve_dev_frontend_launcher(
                backend_host=host,
                no_portless=no_portless,
                tailscale=tailscale,
                funnel=funnel,
                force_takeover=portless_force,
            )
        except DevFrontendConfigurationError as exc:
            print(f"Error: {exc}", file=output, flush=True)
            sys.exit(1)

        resolved_frontend_root = resolve_dev_frontend_root(
            explicit=frontend_root, project_root=project_root
        )
        if resolved_frontend_root is None:
            print(
                "Error: Dev frontend checkout not found.\n"
                "\n"
                "To fix this, either:\n"
                "  1. Pass a checkout: meridian chat --dev --frontend-root /path/to/meridian-web\n"
                "  2. Set MERIDIAN_DEV_FRONTEND_ROOT=/path/to/meridian-web\n"
                "  3. Place meridian-web next to meridian-cli as ../meridian-web\n"
                "  4. Run headless (API only): meridian chat --headless\n",
                file=output,
                flush=True,
            )
            sys.exit(1)
        prerequisite_error = validate_dev_prerequisites(resolved_frontend_root)
        if prerequisite_error is not None:
            print(f"Error: {prerequisite_error}", file=output, flush=True)
            sys.exit(1)

        if host in ("0.0.0.0", "::"):
            print(
                "Warning: Backend is bound to all interfaces. "
                "The frontend sharing mode does not restrict backend API access.",
                file=output,
                flush=True,
            )

        _write_server_discovery(
            host=display_host,
            port=actual_port,
            runtime_root=discovery_runtime_root,
        )
        supervisor = DevSupervisor(
            backend_host=host,
            backend_port=actual_port,
            frontend_root=resolved_frontend_root,
            chat_app=chat_app,
            open_browser=open_browser,
            launcher=launcher,
        )
        try:
            exit_code = asyncio.run(supervisor.run())
        except FrontendLaunchError as exc:
            print(f"Error: {exc}", file=output, flush=True)
            sys.exit(1)
        if exit_code != 0:
            sys.exit(exit_code)
        return actual_port

    if not headless:
        assets = resolve_frontend_assets(
            explicit_dist=Path(frontend_dist) if frontend_dist else None,
            project_root=project_root,
        )
        if assets is None:
            if frontend_dist is not None:
                print(
                    "Error: Built frontend assets not found at the specified path.\n"
                    "\n"
                    "To fix this, either:\n"
                    "  1. Build assets: cd ../meridian-web && pnpm build\n"
                    "  2. Correct the path: meridian chat --frontend-dist /path/to/dist\n"
                    "  3. Run headless (API only): meridian chat --headless\n",
                    file=output,
                    flush=True,
                )
                sys.exit(1)
            print(
                "Note: Frontend assets not found. Running in headless mode.\n"
                "To serve the UI, build assets first: cd ../meridian-web && pnpm build",
                file=output,
                flush=True,
            )
            headless = True
        else:
            from meridian.lib.chat.server import mount_frontend

            mount_frontend(chat_app, assets)
            _check_stale_assets(assets, output)
            print(f"Chat UI: {url}", file=output, flush=True)

    if headless:
        print(f"Chat backend: {url}", file=output, flush=True)

    _write_server_discovery(
        host=display_host,
        port=actual_port,
        runtime_root=discovery_runtime_root,
    )
    if not headless and open_browser:
        webbrowser.open(url)

    runner = uvicorn_run or uvicorn.run
    runner(chat_app, host=host, port=actual_port)
    return actual_port


def _prepare_chat_runtime_read_entrypoint() -> ChatEntryPoint:
    """Prepare the minimal chat entrypoint seam for read-only chat clients."""

    prepared = prepare_for_runtime_read(require_established_project_root())
    return build_chat_entrypoint(prepared)


def _resolve_chat_project_root(entrypoint: ChatEntryPoint | None) -> Path:
    """Resolve project root from a prebuilt seam or project discovery."""

    if entrypoint is not None:
        return _chat_project_root(entrypoint)
    return require_established_project_root()


def _chat_project_root(entrypoint: ChatEntryPoint) -> Path:
    """Extract the required project root from a chat entrypoint."""

    project_root = entrypoint.context.project_root
    if project_root is None:
        raise ValueError("Chat entrypoint is missing project root.")
    return project_root


def _check_stale_assets(assets: FrontendAssets, output: Any) -> None:
    """Warn if checkout-local frontend source appears newer than built assets."""

    try:
        source_dir = require_established_project_root().parent / "meridian-web" / "src"
        if not source_dir.is_dir():
            return

        dist_mtime = assets.index_html.stat().st_mtime
        for pattern in ("*.ts", "*.tsx"):
            for source_file in source_dir.rglob(pattern):
                if source_file.stat().st_mtime > dist_mtime:
                    print(
                        "Warning: Frontend source is newer than built assets. "
                        "Consider rebuilding: cd ../meridian-web && pnpm build",
                        file=output,
                        flush=True,
                    )
                    return
    except BaseException:
        return


def _resolve_harness_id(harness: str | None) -> HarnessId:
    raw = (harness or HarnessId.CLAUDE.value).strip().lower()
    try:
        return HarnessId(raw)
    except ValueError as exc:
        valid = ", ".join(item.value for item in HarnessId)
        raise ValueError(f"unsupported chat harness {raw!r}; expected one of: {valid}") from exc


def _resolve_chat_policy_snapshot(
    *,
    project_root: Path,
    model: str | None,
    harness: str | None,
    agent: str | None,
    skills: tuple[str, ...],
    approval: str | None,
    sandbox: str | None,
    effort: str | None,
    autocompact: int | None,
    autocompact_pct: int | None = None,
) -> ChatPolicySnapshot:
    normalized_harness: str | None = None
    if (harness or "").strip():
        normalized_harness = _resolve_harness_id(harness).value
    config = load_config(project_root)
    catalog = CatalogSession(project_root)
    harness_registry = get_default_harness_registry()
    cli_overrides = RuntimeOverrides(
        model=(model or "").strip() or None,
        harness=normalized_harness,
        agent=(agent or "").strip() or None,
        approval=(approval or "").strip() or None,
        sandbox=(sandbox or "").strip() or None,
        effort=(effort or "").strip() or None,
        autocompact=autocompact,
        autocompact_pct=autocompact_pct,
    )
    policy = resolve_launch_policy(
        SurfacePolicyInput(
            surface=LaunchCompositionSurface.PRIMARY,
            catalog=catalog,
            layers=(cli_overrides, RuntimeOverrides.from_env()),
            config_overrides=RuntimeOverrides.from_config(config),
            config=config,
            harness_registry=harness_registry,
            skills_readonly=True,
            requested_skills=skills,
            supported_execution_policy_fields=frozenset(
                {"effort", "sandbox", "approval", "autocompact"}
            ),
        )
    )
    return snapshot_from_resolved_policy(policy)


def _emit_chat_policy_warnings(warnings: tuple[CompositionWarning, ...], output: Any) -> None:
    for warning in warnings:
        message = warning.message.strip()
        if not message:
            continue
        print(f"Warning ({warning.code}): {message}", file=output, flush=True)


def _find_free_port(host: str) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def _write_server_discovery(*, host: str, port: int, runtime_root: Path | None = None) -> None:
    path = _server_discovery_path(runtime_root=runtime_root)
    display_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    payload = {"host": host, "port": port, "url": f"http://{display_host}:{port}"}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def _server_discovery_path(*, runtime_root: Path | None = None) -> Path:
    if runtime_root is not None:
        return runtime_root / CHAT_SERVER_FILE
    return get_user_home() / CHAT_SERVER_FILE


def _resolve_server_url(url: str | None, *, runtime_root: Path | None = None) -> str:
    if url is not None and url.strip():
        return url.rstrip("/")
    path = _server_discovery_path(runtime_root=runtime_root)
    if not path.exists():
        raise ValueError("chat server URL not found; start `meridian chat` or pass --url")
    try:
        data = cast("object", json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"chat server discovery file is invalid: {path}") from exc
    discovered = cast("dict[str, object]", data).get("url") if isinstance(data, dict) else None
    if not isinstance(discovered, str) or not discovered.strip():
        raise ValueError(f"chat server discovery file is missing url: {path}")
    return discovered.rstrip("/")


def _request_json(
    method: str,
    path: str,
    *,
    url: str | None,
    runtime_root: Path | None = None,
) -> dict[str, object]:
    import httpx

    base_url = _resolve_server_url(url, runtime_root=runtime_root)
    try:
        response = httpx.request(method, f"{base_url}{path}", timeout=5.0)
    except httpx.HTTPError as exc:
        raise ValueError(f"failed to connect to chat server at {base_url}: {exc}") from exc
    if response.status_code >= 400:
        raise ValueError(f"chat server returned HTTP {response.status_code}: {response.text}")
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("invalid chat server response: expected object")
    return cast("dict[str, object]", payload)


def _events_from_response(response: dict[str, object]) -> list[dict[str, object]]:
    raw = response.get("events", [])
    if not isinstance(raw, list):
        raise ValueError("invalid chat server response: events must be a list")
    raw_events = cast("list[object]", raw)
    events: list[dict[str, object]] = []
    for event in raw_events:
        if isinstance(event, dict):
            events.append(cast("dict[str, object]", event))
    return events


def _format_chat_table(rows: list[dict[str, object]]) -> str:
    headers = ("chat_id", "state", "created_at")
    values = [[str(row.get(column) or "") for column in headers] for row in rows]
    widths = [len(header) for header in headers]
    for row in values:
        for index, value in enumerate(row):
            widths[index] = max(widths[index], len(value))
    lines = ["  ".join(header.ljust(widths[index]) for index, header in enumerate(headers))]
    lines.append("  ".join("-" * width for width in widths))
    lines.extend(
        "  ".join(value.ljust(widths[index]) for index, value in enumerate(row)) for row in values
    )
    return "\n".join(lines)


def _format_event_summary(event: dict[str, object]) -> str:
    seq = event.get("seq", "?")
    event_type = event.get("type", "unknown")
    timestamp = event.get("timestamp", "")
    payload = event.get("payload")
    if isinstance(payload, dict) and payload:
        return f"#{seq} {timestamp} {event_type} {json.dumps(payload, sort_keys=True)}"
    return f"#{seq} {timestamp} {event_type}"


def _last_seq(events: list[dict[str, object]]) -> int | None:
    for event in reversed(events):
        seq = event.get("seq")
        if isinstance(seq, int):
            return seq
    return None


async def _follow_chat_log(
    chat_id: str,
    *,
    url: str | None,
    last_seq: int | None,
    runtime_root: Path | None = None,
) -> None:
    import websockets

    base_url = _resolve_server_url(url, runtime_root=runtime_root)
    parsed = urlparse(base_url)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    netloc = parsed.netloc
    ws_url = f"{scheme}://{netloc}/ws/chat/{chat_id}"
    if last_seq is not None:
        ws_url = f"{ws_url}?last_seq={last_seq}"
    async with websockets.connect(ws_url) as websocket:
        async for message in websocket:
            print(message, flush=True)


@dataclass(frozen=True)
class _ChatBackendAcquisitionFactory:
    policy_snapshot: ChatPolicySnapshot

    def build(
        self,
        *,
        pipeline_lookup: PipelineLookup,
        project_root: Path,
        runtime_root: Path,
    ) -> ColdSpawnAcquisition:
        return _build_backend_acquisition(
            runtime_root=runtime_root,
            project_root=project_root,
            pipeline_lookup=pipeline_lookup,
        )


def _build_backend_acquisition(
    *,
    runtime_root: Path,
    project_root: Path,
    pipeline_lookup: PipelineLookup,
) -> ColdSpawnAcquisition:
    manager = SpawnManager(runtime_root=runtime_root, project_root=project_root)
    harness_registry = get_default_harness_registry()

    return ColdSpawnAcquisition(
        spawn_manager=cast("Any", manager),
        normalizer_factory=_chat_policy_normalizer_factory(pipeline_lookup),
        pipeline_lookup=pipeline_lookup,
        launch_plan_factory=lambda chat_id, prompt: _build_chat_launch_plan(
            chat_id=chat_id,
            prompt=prompt,
            pipeline_lookup=pipeline_lookup,
            harness_registry=harness_registry,
            project_root=project_root,
            runtime_root=runtime_root,
        ),
    )


def _normalizer_factory(harness_id: HarnessId) -> Callable[[str, str], EventNormalizer]:
    factory = get_normalizer_factory(harness_id)

    def wrapper(chat_id: str, execution_id: str) -> EventNormalizer:
        return factory(chat_id, execution_id)

    return wrapper


def _chat_policy_normalizer_factory(
    pipeline_lookup: PipelineLookup,
) -> Callable[[str, str], EventNormalizer]:
    def wrapper(chat_id: str, execution_id: str) -> EventNormalizer:
        snapshot = pipeline_lookup.get_policy_snapshot(chat_id)
        return _normalizer_factory(HarnessId(snapshot.harness))(chat_id, execution_id)

    return wrapper


def _build_chat_launch_plan(
    *,
    chat_id: str,
    prompt: str,
    pipeline_lookup: PipelineLookup,
    harness_registry: HarnessRegistry,
    project_root: Path,
    runtime_root: Path,
) -> ChatBackendLaunchPlan:
    snapshot = pipeline_lookup.get_policy_snapshot(chat_id)
    adapter = harness_registry.get_subprocess_harness(HarnessId(snapshot.harness))
    return build_chat_backend_launch_plan(
        snapshot=snapshot,
        initial_prompt=prompt,
        spawn_id=SpawnId(f"chat-{uuid4()}"),
        adapter=adapter,
        project_root=project_root,
        runtime_root=runtime_root,
    )


__all__ = ["register_chat_command", "run_chat_server"]
