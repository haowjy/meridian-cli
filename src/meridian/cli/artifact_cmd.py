"""CLI handlers for ``meridian artifact``."""

from __future__ import annotations

import json
import sys
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Never

from cyclopts import Parameter

from meridian.cli.app_tree import artifact_app
from meridian.lib.artifact import service
from meridian.lib.artifact.store import Serve, load_serves

_DEFAULT_TTL = f"{service.DEFAULT_TTL_SECONDS // 3600}h"


@artifact_app.default
def cmd_artifact_root() -> None:
    """Show artifact serving help."""
    print(
        """Serve static artifacts over Tailscale.

Commands:
  meridian artifact serve [dir]   Serve a directory (default TTL: 2h)
  meridian artifact list          List recorded artifact serves
  meridian artifact stop <slug>   Stop one artifact serve
  meridian artifact gc            Stop expired artifact serves"""
    )
    raise SystemExit(0)


def _fail(message: str, code: int = 1) -> Never:
    print(f"Error: {message}", file=sys.stderr)
    raise SystemExit(code)


def _duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds >= 86400:
        return f"{seconds / 86400:.1f}d"
    if seconds >= 3600:
        return f"{seconds / 3600:.1f}h"
    if seconds >= 60:
        return f"{seconds / 60:.1f}m"
    return f"{seconds}s"


def _created_at(serve: Serve) -> datetime:
    try:
        return datetime.fromisoformat(serve.created.removesuffix("Z") + "+00:00")
    except ValueError:
        return datetime.now(UTC)


@artifact_app.command(name="serve")
def cmd_artifact_serve(
    directory: Annotated[Path, Parameter(help="Directory to serve.")] = Path("."),
    *,
    ttl: Annotated[
        str, Parameter(name="--ttl", help="Lifetime: 30m, 2h, 90s, or 1d.")
    ] = _DEFAULT_TTL,
    persistent: Annotated[
        bool, Parameter(name="--persistent", help="Never expire automatically.")
    ] = False,
    funnel: Annotated[
        bool, Parameter(name="--funnel", help="Expose publicly with Tailscale Funnel.")
    ] = False,
    slug: Annotated[
        str | None, Parameter(name="--slug", help="URL path name (default: directory name).")
    ] = None,
    port: Annotated[
        int | None,
        Parameter(name="--port", help="Serve on this port instead of a random one."),
    ] = None,
) -> None:
    """Serve a directory over Tailscale."""
    resolved = directory.expanduser().resolve()
    if not resolved.exists():
        _fail(f"directory not found: {directory}", 2)
    if not resolved.is_dir():
        _fail(f"not a directory: {directory}", 2)
    if port is not None:
        if port < 1 or port > 65535:
            _fail("--port must be between 1 and 65535", 2)
        if port < 1024:
            _fail("--port must be >= 1024 (do not claim well-known ports)", 2)
    try:
        ttl_seconds = None if persistent else service.parse_ttl(ttl)
        serve, url = service.start_serve(
            resolved, ttl_seconds=ttl_seconds, funnel=funnel, slug=slug, port=port
        )
    except service.PortInUseError as exc:
        _fail(str(exc), 2)
    except (service.ArtifactError, OSError) as exc:
        _fail(str(exc))
    ttl_hint = "persistent" if serve.ttl_seconds is None else _duration(serve.ttl_seconds)
    print(url)
    print(
        f"pid {serve.pid} · TTL {ttl_hint} · stop with: meridian artifact stop {serve.slug}"
    )


@artifact_app.command(name="list")
def cmd_artifact_list(
    *,
    fmt: Annotated[
        str, Parameter(name="--format", help="Output format: text or json.")
    ] = "text",
) -> None:
    """List recorded artifact serves without changing state."""
    from meridian.cli.main import get_global_options

    serves = load_serves()
    effective_fmt = "json" if get_global_options().output.format == "json" else fmt
    if effective_fmt == "json":
        print(json.dumps([asdict(serve) for serve in serves], indent=2))
        return
    if effective_fmt != "text":
        _fail("--format must be text or json", 2)
    if not serves:
        print("No active artifact serves.")
        return
    now = datetime.now(UTC)
    try:
        dns_name = service.tailscale_dns_name()
    except (service.ArtifactError, OSError) as exc:
        dns_name = None
        print(f"Warning: artifact URLs unavailable: {exc}", file=sys.stderr)
    print(f"{'SLUG':<22} {'LOCAL':<7} {'ALIVE':<6} {'AGE':<8} {'TTL':<11} {'DIRECTORY':<30} URL")
    for serve in serves:
        created = _created_at(serve)
        age = _duration((now - created).total_seconds())
        ttl = (
            "persistent"
            if serve.ttl_seconds is None
            else _duration(serve.ttl_seconds - (now - created).total_seconds())
        )
        public_port = service.tailnet_port(serve.local_port, funnel=serve.funnel)
        url = (
            f"https://{dns_name}:{public_port}/{serve.slug}/"
            if dns_name
            else "unavailable"
        )
        alive = (
            "yes"
            if service.pid_alive(serve.pid, serve.process_created_at)
            else "no"
        )
        print(
            f"{serve.slug:<22} {serve.local_port:<7} {alive:<6} {age:<8} "
            f"{ttl:<11} {serve.dir:<30} {url}"
        )


@artifact_app.command(name="stop")
def cmd_artifact_stop(
    slug: Annotated[str, Parameter(help="Slug (or local port) of the serve to stop.")],
) -> None:
    """Stop one artifact serve."""
    try:
        stopped = service.stop_serve(slug)
    except (service.ArtifactError, OSError) as exc:
        _fail(str(exc))
    if stopped:
        print(f"Stopped artifact serve {slug}.")
    else:
        print(f"No artifact serve recorded as {slug}.")


@artifact_app.command(name="gc")
def cmd_artifact_gc() -> None:
    """Stop and remove expired artifact serves."""
    try:
        expired, failed = service.sweep_expired()
    except (service.ArtifactError, OSError) as exc:
        _fail(str(exc))
    print(f"Garbage-collected {len(expired)} expired artifact serve(s).")
    if failed:
        print(f"Failed to remove: {', '.join(failed)}", file=sys.stderr)
        raise SystemExit(1)


__all__ = [
    "cmd_artifact_gc",
    "cmd_artifact_list",
    "cmd_artifact_root",
    "cmd_artifact_serve",
    "cmd_artifact_stop",
]
