"""The launch rules of `ensure_service`: what a launch costs and which unit it may touch."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from meridian.lib.autosync import service

if TYPE_CHECKING:
    import pytest


@dataclass
class _StubManager:
    """Stands in for systemd: a unit file in a temp dir and a log of what was started."""

    home: Path
    path: Path
    kind: str = "systemd"
    unit: str = "meridian-sync"
    log: str = "journalctl"
    calls: list[str] = field(default_factory=list)

    def render(self, binary: str) -> bytes:
        return f'# meridian-sync-unit: {service.TEMPLATE_V}\nExecStart="{binary}"\n'.encode()

    def read(self) -> service._Installed | None:  # pyright: ignore[reportPrivateUsage]
        if not self.path.exists():
            return None
        text = self.path.read_text()
        template = int(text.split("unit: ", 1)[1].split("\n", 1)[0])
        return service._Installed(template, text.split('"')[1])  # pyright: ignore[reportPrivateUsage]

    def activate(self, *, wrote: bool) -> None:
        self.calls.append("activate-after-write" if wrote else "activate")

    def deactivate(self) -> None:
        self.calls.append("deactivate")


def test_launch_rules(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    account = tmp_path / "account"
    home = account / ".meridian"
    home.mkdir(parents=True)
    unit = tmp_path / "unit.service"
    binary = tmp_path / "meridian-sync"
    binary.write_text("")
    mgr = _StubManager(home, unit)
    running = False
    monkeypatch.setenv("MERIDIAN_HOME", str(home))
    monkeypatch.setattr(service, "_account_home", lambda: account)
    monkeypatch.setattr(service, "current_manager", lambda **_: mgr)
    monkeypatch.setattr(service, "is_dev_install", lambda: False)
    monkeypatch.setattr(service, "installed_executable", lambda _name: str(binary))
    monkeypatch.setattr(service, "daemon_running", lambda: running)

    # A missing unit is written and started; the next launch with a daemon does nothing.
    assert service.ensure_service(auto=True).action == "installed"
    assert mgr.calls == ["activate-after-write"]
    running = True
    mgr.calls.clear()
    assert service.ensure_service(auto=True).action == "unchanged"
    assert mgr.calls == []

    # A stopped daemon restarts without rewriting the file.
    running = False
    assert service.ensure_service(auto=True).action == "started"
    assert mgr.calls == ["activate"]

    # An older template is rewritten; a newer one is left alone (another install's unit).
    mgr.calls.clear()
    unit.write_text(unit.read_text().replace(f"unit: {service.TEMPLATE_V}", "unit: 0"))
    assert service.ensure_service(auto=True).action == "updated"
    unit.write_text(unit.read_text().replace(f"unit: {service.TEMPLATE_V}", "unit: 99"))
    running = True
    assert service.ensure_service(auto=True).action == "unchanged"
    assert "unit: 99" in unit.read_text()

    # A launch never touches the default unit from a dev install, `service.off` or another
    # home.
    mgr.calls.clear()
    running = False
    monkeypatch.setattr(service, "is_dev_install", lambda: True)
    assert service.ensure_service(auto=True).reason == "a dev install"
    monkeypatch.setattr(service, "is_dev_install", lambda: False)
    service.service_off_path().parent.mkdir(parents=True, exist_ok=True)
    service.service_off_path().write_text("off")
    assert service.ensure_service(auto=True).reason == "autosync/service.off exists"
    service.service_off_path().unlink()
    other = tmp_path / "other-home"
    other.mkdir()
    monkeypatch.setenv("MERIDIAN_HOME", str(other))
    mgr.home = other
    assert service.ensure_service(auto=True).reason == "a non-default home"
    assert mgr.calls == []
