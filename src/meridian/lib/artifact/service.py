"""Operations for detached static artifact serves."""

from __future__ import annotations

import json
import os
import random
import re
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Collection
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import psutil

from meridian.lib.artifact.store import (
    Serve,
    is_valid_slug,
    load_serves,
    save_serves,
    serves_lock_path,
)
from meridian.lib.platform import IS_WINDOWS
from meridian.lib.platform.locking import lock_file
from meridian.lib.platform.terminate import terminate_tree_sync

DEFAULT_TTL_SECONDS = 2 * 60 * 60
FUNNEL_PORT = 10000
PORT_MIN = 49152
PORT_MAX = 65535


class ArtifactError(RuntimeError):
    """An artifact operation could not be completed."""


class PortInUseError(ArtifactError):
    """An explicitly requested port is unavailable."""


def parse_ttl(value: str) -> int:
    """Parse a positive duration with an s, m, h, or d suffix."""
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    if len(value) < 2 or value[-1].lower() not in units:
        raise ValueError("TTL must be a positive duration such as 30m, 2h, 90s, or 1d")
    try:
        amount = int(value[:-1])
    except ValueError as exc:
        raise ValueError("TTL must be a positive duration such as 30m, 2h, 90s, or 1d") from exc
    if amount <= 0:
        raise ValueError("TTL must be greater than zero")
    return amount * units[value[-1].lower()]


def _parse_created(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError:
        return None
    return parsed.astimezone(UTC)


def is_expired(serve: Serve, now: datetime) -> bool:
    """Return whether a non-persistent serve is strictly past its expiry."""
    if serve.ttl_seconds is None:
        return False
    created = _parse_created(serve.created)
    return created is not None and created + timedelta(seconds=serve.ttl_seconds) < now


def local_port_available(port: int) -> bool:
    """Probe whether localhost can bind a TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def pick_port(
    occupied: Collection[int],
    bind_probe: Callable[[int], bool] = local_port_available,
    *,
    attempts: int = 20,
    choose: Callable[[int, int], int] = random.randint,
) -> int:
    """Pick an unused random high port."""
    for _ in range(attempts):
        port = choose(PORT_MIN, PORT_MAX)
        if port not in occupied and bind_probe(port):
            return port
    raise ArtifactError(f"could not find a free port after {attempts} attempts")


def _tailscale(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["tailscale", *args], capture_output=True, text=True, check=False
    )


def tailscale_dns_name() -> str:
    """Return this node's MagicDNS name."""
    result = _tailscale(["status", "--json"])
    if result.returncode != 0:
        raise ArtifactError(result.stderr.strip() or "tailscale status failed")
    try:
        payload: object = json.loads(result.stdout)
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ArtifactError("tailscale status did not include Self.DNSName") from exc
    if not isinstance(payload, dict):
        raise ArtifactError("tailscale status did not include Self.DNSName")
    self_status = cast("dict[str, object]", payload).get("Self")
    if not isinstance(self_status, dict):
        raise ArtifactError("tailscale status did not include Self.DNSName")
    dns_name = cast("dict[str, object]", self_status).get("DNSName")
    if not isinstance(dns_name, str) or not dns_name.rstrip("."):
        raise ArtifactError("tailscale status did not include Self.DNSName")
    return dns_name.rstrip(".")


def tailscale_serve_ports() -> set[int]:
    """Return configured Tailscale TCP listener ports, or empty on failure."""
    try:
        result = _tailscale(["serve", "status", "--json"])
    except OSError:
        return set()
    if result.returncode != 0:
        return set()
    try:
        payload: object = json.loads(result.stdout)
    except json.JSONDecodeError:
        return set()
    if not isinstance(payload, dict):
        return set()
    tcp = cast("dict[str, object]", payload).get("TCP")
    if not isinstance(tcp, dict):
        return set()
    ports: set[int] = set()
    for value in cast("dict[object, object]", tcp):
        if not isinstance(value, (str, int)) or isinstance(value, bool):
            continue
        try:
            port = int(value)
        except ValueError:
            continue
        if 1 <= port <= 65535:
            ports.add(port)
    return ports


def launch_http_server(directory: Path, port: int, slug: str) -> subprocess.Popen[Any]:
    """Launch a fully detached localhost static server."""
    if not is_valid_slug(slug):
        raise ArtifactError("refusing to launch a server with an invalid artifact slug")
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if IS_WINDOWS:
        # Win32 DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP.
        kwargs["creationflags"] = 0x00000008 | 0x00000200
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "meridian.lib.artifact._serve_dir",
            str(directory),
            str(port),
            slug,
        ],
        **kwargs,
    )


def process_created_epoch(pid: int) -> float:
    """Read a process birth time immediately after launch."""
    return psutil.Process(pid).create_time()


def terminate_pid(pid: int, process_created_at: float | None = None) -> None:
    """Terminate a detached server process tree."""
    terminate_tree_sync(
        pid,
        created_at_epoch=process_created_at or 0.0,
        grace_secs=2.0,
        reason="artifact_stop",
    )


def pid_alive(pid: int, process_created_at: float | None = None) -> bool:
    """Return whether PID still identifies the process that was launched."""
    try:
        process = psutil.Process(pid)
        return process_created_at is None or abs(process.create_time() - process_created_at) <= 1.0
    except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
        return False


def derive_slug(
    directory: Path,
    override: str | None,
    occupied: Collection[str],
    local_port: int,
) -> str:
    """Derive a safe, unique URL path component."""
    source = override if override is not None else directory.name
    base = re.sub(r"-+", "-", re.sub(r"[^a-z0-9-]", "-", source.lower())).strip("-")
    base = base[:64].rstrip("-")
    if not is_valid_slug(base) or source in {".", "..", "/"}:
        raise ArtifactError("slug must contain at least one letter or number")
    candidate = base
    suffix = str(local_port)
    attempt = 1
    while candidate in occupied:
        current_suffix = suffix if attempt == 1 else f"{suffix}-{attempt}"
        candidate = f"{base[: 63 - len(current_suffix)].rstrip('-')}-{current_suffix}"
        attempt += 1
    if not is_valid_slug(candidate):
        raise ArtifactError("could not derive a safe artifact slug")
    return candidate


def _tailscale_register(local_port: int, slug: str, *, funnel: bool) -> None:
    if not is_valid_slug(slug):
        raise ArtifactError("refusing to register an invalid artifact slug")
    command = "funnel" if funnel else "serve"
    public_port = tailnet_port(local_port, funnel=funnel)
    result = _tailscale(
        [
            command,
            "--bg",
            f"--https={public_port}",
            "--set-path",
            f"/{slug}",
            f"http://127.0.0.1:{local_port}",
        ]
    )
    if result.returncode != 0:
        message = result.stderr.strip() or f"tailscale {command} failed"
        if funnel:
            message += f"; public Funnel setup on port {FUNNEL_PORT} failed"
        raise ArtifactError(message)


def tailnet_port(local_port: int, *, funnel: bool) -> int:
    """Return the externally exposed HTTPS port for a serve."""
    return FUNNEL_PORT if funnel else local_port


def tailscale_off(slug: str, public_port: int, *, funnel: bool) -> bool:
    """Remove only one Tailscale Serve/Funnel listener."""
    if not is_valid_slug(slug):
        raise ArtifactError("refusing to remove an invalid artifact slug")
    if not 1 <= public_port <= 65535:
        raise ArtifactError("refusing to remove an invalid artifact port")
    command = "funnel" if funnel else "serve"
    args = [command, f"--https={public_port}", f"--set-path=/{slug}", "off"]
    try:
        result = _tailscale(args)
    except OSError as exc:
        print(str(exc), file=sys.stderr)
        return False
    if result.returncode == 0:
        return True
    if funnel and result.returncode != 0:
        try:
            fallback = _tailscale(
                ["serve", f"--https={public_port}", f"--set-path=/{slug}", "off"]
            )
        except OSError as exc:
            print(str(exc), file=sys.stderr)
            return False
        if fallback.returncode == 0:
            return True
        message = fallback.stderr.strip() or result.stderr.strip()
        if message:
            print(message, file=sys.stderr)
        return False
    if result.stderr.strip():
        print(result.stderr.strip(), file=sys.stderr)
    return False


def build_url(slug: str, public_port: int) -> str:
    if not is_valid_slug(slug):
        raise ArtifactError("refusing to build a URL for an invalid artifact slug")
    return f"https://{tailscale_dns_name()}:{public_port}/{slug}/"


def _now_utc() -> datetime:
    return datetime.now(UTC)


def wait_for_http_server(
    proc: subprocess.Popen[Any], port: int, *, timeout: float = 2.0
) -> bool:
    """Poll until the child accepts localhost TCP connections or fails."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return False
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                return True
        except OSError:
            time.sleep(0.05)
    return False


def _sweep_expired_locked(now: datetime) -> tuple[list[Serve], list[str]]:
    serves = load_serves()
    expired = [serve for serve in serves if is_expired(serve, now)]
    if not expired:
        save_serves(serves)
        return [], []
    removed: list[Serve] = []
    failed: list[str] = []
    for serve in expired:
        public_port = tailnet_port(serve.local_port, funnel=serve.funnel)
        if not tailscale_off(serve.slug, public_port, funnel=serve.funnel):
            failed.append(serve.slug)
            continue
        try:
            terminate_pid(serve.pid, serve.process_created_at)
        except OSError:
            failed.append(serve.slug)
            continue
        removed.append(serve)
    removed_slugs = {serve.slug for serve in removed}
    save_serves([serve for serve in serves if serve.slug not in removed_slugs])
    return removed, failed


def sweep_expired(*, now: datetime | None = None) -> tuple[list[Serve], list[str]]:
    """Stop expired serves and persist the surviving entries once."""
    with lock_file(serves_lock_path()):
        return _sweep_expired_locked(now or _now_utc())


def start_serve(
    directory: Path,
    *,
    ttl_seconds: int | None,
    funnel: bool,
    slug: str | None = None,
    port: int | None = None,
) -> tuple[Serve, str]:
    """Launch, expose, and record a detached artifact server."""
    with lock_file(serves_lock_path()):
        _sweep_expired_locked(_now_utc())
        serves = load_serves()
        occupied_ports = {
            serve.local_port for serve in serves
        } | tailscale_serve_ports()
        if port is not None and (
            port in occupied_ports or not local_port_available(port)
        ):
            raise PortInUseError(f"port {port} is already in use")
        proc: subprocess.Popen[Any] | None = None
        local_port = 0
        resolved_slug = ""
        process_created_at: float | None = None
        attempts = 1 if port is not None else 3
        for _ in range(attempts):
            local_port = port if port is not None else pick_port(occupied_ports)
            occupied_ports.add(local_port)
            resolved_slug = derive_slug(
                directory, slug, {serve.slug for serve in serves}, local_port
            )
            proc = launch_http_server(directory, local_port, resolved_slug)
            try:
                process_created_at = process_created_epoch(proc.pid)
            except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
                terminate_pid(proc.pid)
                proc = None
                continue
            if wait_for_http_server(proc, local_port):
                break
            terminate_pid(proc.pid, process_created_at)
            proc = None
        if proc is None:
            raise ArtifactError(
                f"static HTTP server did not become ready after {attempts} attempt(s)"
            )
        if process_created_at is None:
            raise ArtifactError("could not record static HTTP server process identity")

        created = _now_utc().isoformat().replace("+00:00", "Z")
        serve = Serve(
            slug=resolved_slug,
            local_port=local_port,
            dir=str(directory),
            work_id=os.getenv("MERIDIAN_ACTIVE_WORK_ID") or None,
            created=created,
            ttl_seconds=ttl_seconds,
            funnel=funnel,
            pid=proc.pid,
            process_created_at=process_created_at,
        )
        registered = False
        try:
            _tailscale_register(local_port, resolved_slug, funnel=funnel)
            registered = True
            public_port = tailnet_port(local_port, funnel=funnel)
            url = build_url(resolved_slug, public_port)
            save_serves([*serves, serve])
        except Exception:
            cleanup_succeeded = not registered or tailscale_off(
                resolved_slug,
                tailnet_port(local_port, funnel=funnel),
                funnel=funnel,
            )
            if cleanup_succeeded:
                terminate_pid(proc.pid, process_created_at)
            else:
                save_serves([*serves, serve])
            raise
        return serve, url


def stop_serve(ref: str) -> bool:
    """Stop and forget one serve. Return False when it is unknown."""
    with lock_file(serves_lock_path()):
        serves = load_serves()
        serve = next(
            (item for item in serves if item.slug == ref or str(item.local_port) == ref),
            None,
        )
        if serve is None:
            save_serves(serves)
            return False
        public_port = tailnet_port(serve.local_port, funnel=serve.funnel)
        if not tailscale_off(serve.slug, public_port, funnel=serve.funnel):
            raise ArtifactError(f"failed to remove Tailscale path /{serve.slug}")
        terminate_pid(serve.pid, serve.process_created_at)
        save_serves([item for item in serves if item.slug != serve.slug])
        return True


__all__ = [
    "DEFAULT_TTL_SECONDS",
    "FUNNEL_PORT",
    "ArtifactError",
    "PortInUseError",
    "build_url",
    "derive_slug",
    "is_expired",
    "launch_http_server",
    "local_port_available",
    "parse_ttl",
    "pick_port",
    "pid_alive",
    "start_serve",
    "stop_serve",
    "sweep_expired",
    "tailnet_port",
    "tailscale_dns_name",
    "tailscale_off",
    "tailscale_serve_ports",
    "terminate_pid",
]
