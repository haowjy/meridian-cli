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

from pydantic import BaseModel, ConfigDict

import meridian
from meridian.lib.autosync.state import autosync_dir, daemon_running, service_off_path
from meridian.lib.core.util import FormatContext
from meridian.lib.platform import IS_WINDOWS
from meridian.lib.platform.atomic import atomic_replace, atomic_write_text
from meridian.lib.platform.python_module import installed_executable
from meridian.lib.state.user_paths import get_user_home

# Bump when the unit or plist text changes: older files are rewritten, newer ones left alone.
TEMPLATE_V = 1
# A fixed PATH: nothing in the service comes from the launching shell but the binary's path.
SERVICE_PATH = "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin"
# How long `sync service install` waits for the daemon it started to take the lock.
SETTLE_S = 3.0
# Seconds one `systemctl`/`launchctl` call may take: short on a launch, longer on request.
LAUNCH_TIMEOUT_S = 5.0
COMMAND_TIMEOUT_S = 20.0

Action = Literal["installed", "updated", "started", "unchanged", "removed", "skipped"]


class ServiceError(RuntimeError):
    """The service manager refused or failed; the message says what to do."""


class ServiceResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    action: Action
    unit: str | None = None
    binary: str | None = None
    # Why nothing was done, when `action` is "skipped".
    reason: str | None = None
    # Where to look when the daemon doesn't come up.
    log: str | None = None

    def format_text(self, ctx: FormatContext | None = None) -> str:
        unit = f" `{self.unit}`" if self.unit else ""
        if self.action == "skipped":
            return f"Nothing done: {self.reason}."
        if self.action == "removed":
            return (
                f"Removed the service{unit}. Autosync is off on this machine until "
                "`meridian sync service install`."
            )
        verb = {
            "installed": "Installed",
            "updated": "Rewrote",
            "started": "Started",
            "unchanged": "Already running:",
        }[self.action]
        binary = f" ({self.binary})" if self.binary else ""
        return f"{verb} the service{unit}{binary}."


class ServiceStatus(BaseModel):
    """Whether this machine's service manager has the unit; `manager` is `None` where
    there is no user service manager."""

    model_config = ConfigDict(frozen=True)

    manager: Literal["systemd", "launchd"] | None
    unit: str | None = None
    installed: bool = False

    def text(self) -> str:
        if self.manager is None:
            return "no service manager here"
        return f"{self.manager} `{self.unit}`, " + (
            "installed" if self.installed else "not installed"
        )


def is_dev_install() -> bool:
    """A checkout run in place: its package is not under a `site-packages` directory."""

    return "site-packages" not in Path(meridian.__file__).parts


def _home() -> Path:
    return get_user_home().absolute()


def _account_home() -> Path:
    """The login account's home from the passwd database. The user service manager reads
    its units from here whatever `$HOME` the calling shell has, so neither the default-home
    test nor the unit's location may follow `$HOME`."""

    import pwd

    return Path(pwd.getpwuid(os.getuid()).pw_dir)


def _is_default_home(home: Path) -> bool:
    return home == (_account_home() / ".meridian").absolute()


def _suffix(home: Path, sep: str) -> str:
    """Empty for the default home; another home gets its own service, so a test's
    temporary home never touches the real one."""

    if _is_default_home(home):
        return ""
    return sep + hashlib.sha256(str(home).encode("utf-8")).hexdigest()[:8]


@dataclass(frozen=True)
class _Installed:
    """What an existing unit or plist says of itself."""

    template: int | None
    binary: str | None


def _run(
    argv: list[str], *, timeout: float, check: bool = True
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ServiceError(f"`{' '.join(argv)}` failed: {exc}") from exc
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise ServiceError(f"`{' '.join(argv)}` failed: {detail or f'exit {result.returncode}'}")
    return result


def _quote(value: str) -> str:
    """A systemd value: quoted, `\\` and `"` escaped, and `%` doubled (systemd expands it)."""

    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    return f'"{escaped}"'


@dataclass(frozen=True)
class _Systemd:
    """A systemd user unit."""

    home: Path
    timeout: float
    kind: Literal["systemd"] = "systemd"

    @property
    def unit(self) -> str:
        return "meridian-sync" + _suffix(self.home, "-")

    @property
    def path(self) -> Path:
        return _account_home() / ".config" / "systemd" / "user" / f"{self.unit}.service"

    @property
    def log(self) -> str:
        return f"journalctl --user -u {self.unit}"

    def render(self, binary: str) -> bytes:
        return (
            f"# meridian-sync-unit: {TEMPLATE_V}\n"
            "[Unit]\n"
            "Description=Meridian context sync\n"
            f"ConditionPathExists={binary.replace('%', '%%')}\n"
            "[Service]\n"
            f"ExecStart={_quote(binary)} run --home {_quote(str(self.home))}\n"
            "Restart=on-failure\n"
            "RestartSec=10\n"
            "TimeoutStopSec=120\n"
            f"Environment={_quote('PATH=' + SERVICE_PATH)}\n"
            "[Install]\n"
            "WantedBy=default.target\n"
        ).encode()

    def read(self) -> _Installed | None:
        try:
            text = self.path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        template = re.search(r"^# meridian-sync-unit: (\d+)$", text, re.MULTILINE)
        exec_start = re.search(r'^ExecStart="((?:[^"\\]|\\.)*)"', text, re.MULTILINE)
        binary = (
            re.sub(r"\\(.)", r"\1", exec_start.group(1)).replace("%%", "%") if exec_start else None
        )
        return _Installed(int(template.group(1)) if template else None, binary)

    def _ctl(self, *args: str, check: bool = True) -> None:
        _run(["systemctl", "--user", *args], timeout=self.timeout, check=check)

    def activate(self, *, wrote: bool) -> None:
        """Start the service from its file: after a write the manager reads and enables it
        first; `restart` also starts a stopped unit."""

        if wrote:
            self._ctl("daemon-reload")
            self._ctl("enable", f"{self.unit}.service")
        self._ctl("restart", f"{self.unit}.service")

    def deactivate(self) -> None:
        self._ctl("disable", "--now", f"{self.unit}.service", check=False)
        self.path.unlink(missing_ok=True)
        self._ctl("daemon-reload", check=False)


@dataclass(frozen=True)
class _Launchd:
    """A launchd agent."""

    home: Path
    timeout: float
    kind: Literal["launchd"] = "launchd"

    @property
    def unit(self) -> str:
        return "dev.meridian.sync" + _suffix(self.home, ".")

    @property
    def path(self) -> Path:
        return _account_home() / "Library" / "LaunchAgents" / f"{self.unit}.plist"

    @property
    def log(self) -> str:
        return str(self.home / "autosync" / "daemon.log")

    def render(self, binary: str) -> bytes:
        return plistlib.dumps(
            {
                "Label": self.unit,
                "MeridianTemplate": TEMPLATE_V,
                "ProgramArguments": [binary, "run", "--home", str(self.home)],
                "EnvironmentVariables": {"PATH": SERVICE_PATH},
                "RunAtLoad": True,
                "KeepAlive": {"SuccessfulExit": False},
                "ThrottleInterval": 10,
                "StandardErrorPath": self.log,
            }
        )

    def read(self) -> _Installed | None:
        try:
            raw = self.path.read_bytes()
        except OSError:
            return None
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

    def _domain(self) -> str:
        uid = os.getuid()
        gui = f"gui/{uid}"
        probe = _run(["launchctl", "print", gui], timeout=self.timeout, check=False)
        return gui if probe.returncode == 0 else f"user/{uid}"

    def activate(self, *, wrote: bool) -> None:
        domain = self._domain()
        _run(["launchctl", "bootout", f"{domain}/{self.unit}"], timeout=self.timeout, check=False)
        _run(["launchctl", "bootstrap", domain, str(self.path)], timeout=self.timeout)

    def deactivate(self) -> None:
        domain = self._domain()
        _run(["launchctl", "bootout", f"{domain}/{self.unit}"], timeout=self.timeout, check=False)
        self.path.unlink(missing_ok=True)


type _Manager = _Systemd | _Launchd


def current_manager(*, timeout: float = COMMAND_TIMEOUT_S) -> _Manager | None:
    """This machine's user service manager for the current home, or ``None`` (containers,
    WSL without systemd, Windows). Path and `which` checks only: no process is started."""

    if IS_WINDOWS:
        return None
    if sys.platform == "darwin":
        return _Launchd(_home(), timeout) if shutil.which("launchctl") else None
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if (
        shutil.which("systemctl")
        and Path("/run/systemd/system").is_dir()
        and runtime
        and (Path(runtime) / "systemd" / "private").exists()
    ):
        return _Systemd(_home(), timeout)
    return None


def has_manager() -> bool:
    return current_manager() is not None


def _settle() -> None:
    deadline = time.monotonic() + SETTLE_S
    while time.monotonic() < deadline and not daemon_running():
        time.sleep(0.05)


# ---- The entry points --------------------------------------------------------------


def ensure_service(*, auto: bool) -> ServiceResult:
    """Make the service run this install's `meridian-sync`.

    Writes the unit and restarts the service when the file is missing, from an older
    template, or names a binary that is gone; a unit from a newer template or naming
    another install's live binary is left alone. Otherwise restarts the service only when
    no daemon holds the lock. With `auto` (a human's primary launch) it also skips: a dev
    install, a home other than the default, and `service.off`. Raises `ServiceError` when
    the manager fails.
    """

    mgr = current_manager(timeout=LAUNCH_TIMEOUT_S if auto else COMMAND_TIMEOUT_S)
    if mgr is None:
        return ServiceResult(action="skipped", reason="no user service manager")
    if auto:
        if is_dev_install():
            return ServiceResult(action="skipped", reason="a dev install")
        if not _is_default_home(mgr.home):
            return ServiceResult(action="skipped", reason="a non-default home")
        if service_off_path().exists():
            return ServiceResult(action="skipped", reason="autosync/service.off exists")
    binary = installed_executable("meridian-sync")
    if binary is None:
        return ServiceResult(action="skipped", reason="meridian-sync is not installed")

    installed = mgr.read()
    # The binary the unit already runs, when it should be left alone.
    keep = (
        installed.binary
        if installed is not None
        and installed.template is not None
        and installed.template >= TEMPLATE_V
        and installed.binary is not None
        and Path(installed.binary).exists()
        else None
    )
    if keep is None:
        mgr.path.parent.mkdir(parents=True, exist_ok=True)
        autosync_dir().mkdir(parents=True, exist_ok=True)
        with atomic_replace(mgr.path, mode="wb") as handle:
            handle.write(mgr.render(binary))
        mgr.activate(wrote=True)
        action: Action = "installed" if installed is None else "updated"
        return ServiceResult(action=action, unit=mgr.unit, binary=binary, log=mgr.log)
    if not daemon_running():
        mgr.activate(wrote=False)
        return ServiceResult(action="started", unit=mgr.unit, binary=keep, log=mgr.log)
    return ServiceResult(action="unchanged", unit=mgr.unit, binary=keep)


def install_service() -> ServiceResult:
    """`meridian sync service install`: any install and home; undoes `uninstall`, and
    waits for the daemon to take the lock."""

    service_off_path().unlink(missing_ok=True)
    result = ensure_service(auto=False)
    if result.action in ("installed", "updated", "started"):
        _settle()
    return result


def uninstall_service() -> ServiceResult:
    """Stop and remove the service and write `service.off`, which keeps launches from
    installing it again."""

    off = service_off_path()
    off.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(off, "autosync is off on this machine; `meridian sync service install`\n")
    mgr = current_manager()
    if mgr is None:
        return ServiceResult(action="skipped", reason="no user service manager")
    existed = mgr.path.exists()
    mgr.deactivate()
    return ServiceResult(
        action="removed" if existed else "skipped",
        unit=mgr.unit,
        reason=None if existed else "no service was installed",
    )


def service_status() -> ServiceStatus:
    mgr = current_manager()
    if mgr is None:
        return ServiceStatus(manager=None)
    return ServiceStatus(manager=mgr.kind, unit=mgr.unit, installed=mgr.path.exists())
