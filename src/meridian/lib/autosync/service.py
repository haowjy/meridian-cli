"""The user service that keeps `meridian-sync run` alive: a systemd user unit on Linux, a
launchd agent on macOS.

`ensure_service` is the one function that installs, repairs and starts it. The service's
state follows the daemon (the lock probe), not the unit file, so a moved install, a new
template, a stopped service and a deleted `autosync/` directory all end the same way:
the daemon running.
"""

from __future__ import annotations

import hashlib
import os
import plistlib
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import meridian
from meridian.lib.autosync.state import autosync_dir, daemon_running, service_off_path
from meridian.lib.platform import IS_WINDOWS
from meridian.lib.platform.atomic import atomic_replace, atomic_write_text
from meridian.lib.platform.python_module import installed_executable
from meridian.lib.state.user_paths import get_user_home

# Bump when the unit or plist text changes: older files are rewritten.
TEMPLATE_V = 1
# A fixed PATH: nothing in the service comes from the launching shell but the binary's path.
SERVICE_PATH = "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin"
# How long a launch waits for a daemon it just started to take the lock.
SETTLE_S = 3.0

Manager = Literal["systemd", "launchd"]
Action = Literal["installed", "updated", "started", "unchanged", "removed", "skipped"]


class ServiceError(RuntimeError):
    """The service manager refused or failed; the message says what to do."""


@dataclass(frozen=True)
class ServiceResult:
    action: Action
    unit: str | None = None
    binary: str | None = None
    # Why nothing was done, when `action` is "skipped".
    reason: str | None = None


def manager() -> Manager | None:
    """The user service manager of this machine, or ``None`` (containers, WSL without
    systemd, Windows). Path and `which` checks only: no process is started."""

    if IS_WINDOWS:
        return None
    if sys.platform == "darwin":
        return "launchd" if shutil.which("launchctl") else None
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if (
        shutil.which("systemctl")
        and Path("/run/systemd/system").is_dir()
        and runtime
        and (Path(runtime) / "systemd" / "private").exists()
    ):
        return "systemd"
    return None


def is_dev_install() -> bool:
    """A checkout run in place: its package is not under a `site-packages` directory."""

    return "site-packages" not in Path(meridian.__file__).parts


def _home() -> Path:
    return get_user_home().absolute()


def _is_default_home() -> bool:
    return _home() == (Path.home() / ".meridian").absolute()


def _suffix(home: Path, sep: str) -> str:
    """Empty for the default home; another home gets its own service, so a test's
    temporary `MERIDIAN_HOME` never touches the real one."""

    if _is_default_home():
        return ""
    return sep + hashlib.sha256(str(home).encode("utf-8")).hexdigest()[:8]


def unit_name(home: Path | None = None) -> str:
    return "meridian-sync" + _suffix(home or _home(), "-")


def _label(home: Path) -> str:
    return "dev.meridian.sync" + _suffix(home, ".")


def _unit_path(home: Path) -> Path:
    config = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(config) / "systemd" / "user" / f"{unit_name(home)}.service"


def _plist_path(home: Path) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{_label(home)}.plist"


# ---- Rendering ---------------------------------------------------------------------


def _quote(value: str) -> str:
    """A systemd value: quoted, `\\` and `"` escaped, and `%` doubled (systemd expands it)."""

    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    return f'"{escaped}"'


def render_unit(binary: str, home: Path) -> str:
    return (
        f"# meridian-sync-unit: {TEMPLATE_V}\n"
        "[Unit]\n"
        "Description=Meridian context sync\n"
        f"ConditionPathExists={binary.replace('%', '%%')}\n"
        "[Service]\n"
        f"ExecStart={_quote(binary)} run --home {_quote(str(home))}\n"
        "Restart=on-failure\n"
        "RestartSec=10\n"
        "TimeoutStopSec=120\n"
        f"Environment={_quote('PATH=' + SERVICE_PATH)}\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def render_plist(binary: str, home: Path) -> bytes:
    return plistlib.dumps(
        {
            "Label": _label(home),
            "MeridianTemplate": TEMPLATE_V,
            "ProgramArguments": [binary, "run", "--home", str(home)],
            "EnvironmentVariables": {"PATH": SERVICE_PATH},
            "RunAtLoad": True,
            "KeepAlive": {"SuccessfulExit": False},
            "ThrottleInterval": 10,
            "StandardErrorPath": str(home / "autosync" / "daemon.log"),
        }
    )


@dataclass(frozen=True)
class _Installed:
    """What an existing unit or plist says of itself."""

    template: int | None
    binary: str | None


def _read_installed(path: Path, which: Manager) -> _Installed | None:
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if which == "launchd":
        try:
            data = plistlib.loads(raw)
        except Exception:  # an unreadable plist is rewritten like an old one
            return _Installed(None, None)
        args: object = data.get("ProgramArguments")
        binary = args[0] if isinstance(args, list) and args else None  # pyright: ignore[reportUnknownVariableType]
        template: object = data.get("MeridianTemplate")
        return _Installed(
            template if isinstance(template, int) else None,
            binary if isinstance(binary, str) else None,
        )
    text = raw.decode("utf-8", errors="replace")
    template = re.search(r"^# meridian-sync-unit: (\d+)$", text, re.MULTILINE)
    exec_start = re.search(r'^ExecStart="((?:[^"\\]|\\.)*)"', text, re.MULTILINE)
    binary = re.sub(r"\\(.)", r"\1", exec_start.group(1)).replace("%%", "%") if exec_start else None
    return _Installed(int(template.group(1)) if template else None, binary)


# ---- Talking to the manager --------------------------------------------------------


def _run(argv: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=20, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ServiceError(f"`{' '.join(argv)}` failed: {exc}") from exc
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise ServiceError(f"`{' '.join(argv)}` failed: {detail or f'exit {result.returncode}'}")
    return result


def _launchd_domain() -> str:
    uid = os.getuid()
    gui = f"gui/{uid}"
    return gui if _run(["launchctl", "print", gui], check=False).returncode == 0 else f"user/{uid}"


def _load(which: Manager, path: Path, home: Path, *, replacing: bool) -> None:
    """Make the manager read a new or rewritten file and start the service from it."""

    if which == "systemd":
        _run(["systemctl", "--user", "daemon-reload"])
        _run(["systemctl", "--user", "enable", path.name])
        _run(["systemctl", "--user", "restart" if replacing else "start", path.name])
        return
    domain = _launchd_domain()
    if replacing:
        _run(["launchctl", "bootout", f"{domain}/{_label(home)}"], check=False)
    _run(["launchctl", "bootstrap", domain, str(path)])


def _restart(which: Manager, path: Path, home: Path) -> None:
    """Start a service whose file is right but whose daemon isn't running."""

    if which == "systemd":
        _run(["systemctl", "--user", "restart", path.name])
        return
    domain = _launchd_domain()
    if _run(["launchctl", "kickstart", "-k", f"{domain}/{_label(home)}"], check=False).returncode:
        _run(["launchctl", "bootstrap", domain, str(path)])


def _deactivate(which: Manager, path: Path, home: Path) -> None:
    if which == "systemd":
        _run(["systemctl", "--user", "disable", "--now", path.name], check=False)
        path.unlink(missing_ok=True)
        _run(["systemctl", "--user", "daemon-reload"], check=False)
        return
    _run(["launchctl", "bootout", f"{_launchd_domain()}/{_label(home)}"], check=False)
    path.unlink(missing_ok=True)


def _settle() -> None:
    deadline = time.monotonic() + SETTLE_S
    while time.monotonic() < deadline and not daemon_running():
        time.sleep(0.05)


# ---- The two entry points ----------------------------------------------------------


def ensure_service(*, auto: bool) -> ServiceResult:
    """Make the service run this install's `meridian-sync`.

    Writes the unit and restarts the service when the file is missing, from an older
    template, or names a binary that is gone; a unit naming another install's live binary
    is left alone. Otherwise restarts the service only when no daemon holds the lock.
    With `auto` (a human's primary launch) it also skips: a dev install, a home other than
    the default, and `service.off`. Raises `ServiceError` when the manager fails.
    """

    which = manager()
    if which is None:
        return ServiceResult("skipped", reason="no user service manager")
    if auto:
        if is_dev_install():
            return ServiceResult("skipped", reason="a dev install")
        if not _is_default_home():
            return ServiceResult("skipped", reason="a non-default home")
        if service_off_path().exists():
            return ServiceResult("skipped", reason="autosync/service.off exists")
    binary = installed_executable("meridian-sync")
    if binary is None:
        return ServiceResult("skipped", reason="meridian-sync is not installed")

    home = _home()
    path = _unit_path(home) if which == "systemd" else _plist_path(home)
    installed = _read_installed(path, which)
    stale = (
        installed is None
        or installed.template != TEMPLATE_V
        or installed.binary is None
        or not Path(installed.binary).exists()
    )
    unit = path.stem if which == "systemd" else _label(home)
    if stale:
        path.parent.mkdir(parents=True, exist_ok=True)
        autosync_dir().mkdir(parents=True, exist_ok=True)
        if which == "systemd":
            atomic_write_text(path, render_unit(binary, home))
        else:
            with atomic_replace(path, mode="wb") as handle:
                handle.write(render_plist(binary, home))
        _load(which, path, home, replacing=installed is not None)
        _settle()
        return ServiceResult("installed" if installed is None else "updated", unit, binary)
    if not daemon_running():
        _restart(which, path, home)
        _settle()
        return ServiceResult("started", unit, binary)
    return ServiceResult("unchanged", unit, installed.binary if installed else binary)


def uninstall_service() -> ServiceResult:
    """Stop and remove the service and write `service.off`, which keeps launches from
    installing it again."""

    home = _home()
    off = service_off_path()
    off.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(off, "autosync is off on this machine; `meridian sync service install`\n")
    which = manager()
    if which is None:
        return ServiceResult("skipped", reason="no user service manager")
    path = _unit_path(home) if which == "systemd" else _plist_path(home)
    existed = path.exists()
    _deactivate(which, path, home)
    unit = path.stem if which == "systemd" else _label(home)
    return ServiceResult(
        "removed" if existed else "skipped",
        unit,
        reason=None if existed else "no service was installed",
    )


def install_service() -> ServiceResult:
    """`meridian sync service install`: any install and home; undoes `uninstall`."""

    service_off_path().unlink(missing_ok=True)
    return ensure_service(auto=False)


def describe() -> str:
    """One line for `meridian sync status`: is the service installed."""

    which = manager()
    if which is None:
        return "no service manager here"
    home = _home()
    path = _unit_path(home) if which == "systemd" else _plist_path(home)
    name = path.stem if which == "systemd" else _label(home)
    return f"{which} `{name}`, " + ("installed" if path.exists() else "not installed")
