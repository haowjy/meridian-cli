"""Tests for the architecture-check lane entrypoint."""

from __future__ import annotations

from pathlib import Path

import pytest

from meridian.dev import architecture_checks


def _check(
    check_id: str,
    description: str,
    violations: list[str],
) -> architecture_checks.ArchitectureCheck:
    return architecture_checks.ArchitectureCheck(
        check_id=check_id,
        description=description,
        run=lambda _project_root: list(violations),
    )


def test_selected_checks_defaults_to_all_checks() -> None:
    assert architecture_checks._selected_checks(None) == architecture_checks.CHECKS


def test_selected_checks_returns_requested_ids_in_order() -> None:
    selected = architecture_checks._selected_checks(["CONTEXT-01", "LAUNCH-DTO-01", "CONTEXT-01"])

    assert [check.check_id for check in selected] == ["CONTEXT-01", "LAUNCH-DTO-01"]


def test_selected_checks_rejects_unknown_ids() -> None:
    with pytest.raises(ValueError, match="Unknown architecture check id"):
        architecture_checks._selected_checks(["NOT-A-CHECK"])


def test_run_checks_prints_failures_and_returns_nonzero(capsys: pytest.CaptureFixture[str]) -> None:
    result = architecture_checks.run_checks(
        checks=(
            _check("PASS-01", "healthy", []),
            _check("FAIL-01", "drifted", ["b.py:2: second", "a.py:1: first"]),
        )
    )

    assert result == 1
    out = capsys.readouterr().out
    assert "PASS PASS-01 healthy" in out
    assert "FAIL FAIL-01 drifted" in out
    assert "  - a.py:1: first" in out
    assert "  - b.py:2: second" in out
    assert "architecture-check: 1/2 checks failed" in out


def test_main_list_prints_available_checks(capsys: pytest.CaptureFixture[str]) -> None:
    assert architecture_checks.main(["--list"]) == 0

    out = capsys.readouterr().out
    assert "LIFECYCLE-01" in out
    assert "LAUNCH-BOUNDARY-03" in out
    assert "PLAT-04" in out


def test_main_runs_only_selected_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: dict[str, object] = {}

    def fake_run_checks(
        checks: tuple[architecture_checks.ArchitectureCheck, ...],
        *,
        project_root=None,
    ) -> int:
        recorded["ids"] = [check.check_id for check in checks]
        recorded["project_root"] = project_root
        return 0

    monkeypatch.setattr(architecture_checks, "run_checks", fake_run_checks)

    assert architecture_checks.main(["--check", "CONTEXT-01", "--check", "LIFECYCLE-01"]) == 0
    assert recorded == {
        "ids": ["CONTEXT-01", "LIFECYCLE-01"],
        "project_root": None,
    }


def _write(path: Path, contents: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents, encoding="utf-8")


def test_plat04_check_flags_unapproved_os_specific_primitives(tmp_path: Path) -> None:
    _write(
        tmp_path / "src/meridian/lib/ops/policy_module.py",
        """from meridian.lib.platform import IS_WINDOWS
import msvcrt

if IS_WINDOWS:
    pass
""",
    )

    violations = architecture_checks._check_platform_boundary_drift(tmp_path)

    assert any("src/meridian/lib/ops/policy_module.py:1" in violation for violation in violations)
    assert any("IS_WINDOWS" in violation for violation in violations)
    assert any("src/meridian/lib/ops/policy_module.py:2" in violation for violation in violations)
    assert any("import msvcrt" in violation for violation in violations)


def test_plat04_check_flags_import_alias_bypass_forms(tmp_path: Path) -> None:
    _write(
        tmp_path / "src/meridian/lib/ops/alias_bypass.py",
        """from os import name as os_name
from platform import system as platform_system
from sys import platform as sys_platform
from meridian.lib.platform import IS_WINDOWS as windows_flag
import os as _os
import platform as _platform
import msvcrt as _msvcrt
from winreg import OpenKey as _open_key

if os_name == "nt":
    pass
if sys_platform == "win32":
    pass
if _os.name == "nt":
    pass
if platform_system() == "Windows":
    pass
if _platform.system() == "Windows":
    pass
if windows_flag:
    pass
""",
    )

    violations = architecture_checks._check_platform_boundary_drift(tmp_path)

    assert any("src/meridian/lib/ops/alias_bypass.py:10" in violation for violation in violations)
    assert any("os.name" in violation for violation in violations)
    assert any("src/meridian/lib/ops/alias_bypass.py:12" in violation for violation in violations)
    assert any("sys.platform" in violation for violation in violations)
    assert any("src/meridian/lib/ops/alias_bypass.py:14" in violation for violation in violations)
    assert any("src/meridian/lib/ops/alias_bypass.py:16" in violation for violation in violations)
    assert any("platform.system()" in violation for violation in violations)
    assert any("src/meridian/lib/ops/alias_bypass.py:7" in violation for violation in violations)
    assert any("import msvcrt" in violation for violation in violations)
    assert any("src/meridian/lib/ops/alias_bypass.py:8" in violation for violation in violations)
    assert any("import winreg" in violation for violation in violations)
    assert any("src/meridian/lib/ops/alias_bypass.py:20" in violation for violation in violations)
    assert any("IS_WINDOWS" in violation for violation in violations)


def test_plat04_check_allows_approved_adapter_paths(tmp_path: Path) -> None:
    _write(
        tmp_path / "src/meridian/lib/platform/fcntl_adapter.py",
        """from meridian.lib.platform import IS_WINDOWS
import msvcrt
import sys

if IS_WINDOWS or sys.platform == "win32":
    pass
""",
    )

    assert architecture_checks._check_platform_boundary_drift(tmp_path) == []


def test_plat04_check_scopes_launch_process_approvals_to_explicit_files(tmp_path: Path) -> None:
    _write(
        tmp_path / "src/meridian/lib/launch/process/ports.py",
        """import sys

if sys.platform == "win32":
    pass
""",
    )
    _write(
        tmp_path / "src/meridian/lib/launch/process/pty_launcher.py",
        """import sys

if sys.platform == "win32":
    pass
""",
    )

    violations = architecture_checks._check_platform_boundary_drift(tmp_path)

    assert any(
        "src/meridian/lib/launch/process/ports.py:3" in violation for violation in violations
    )
    assert any("sys.platform" in violation for violation in violations)
    assert not any(
        "src/meridian/lib/launch/process/pty_launcher.py" in violation
        for violation in violations
    )


def test_plat04_check_ignores_generic_os_sys_subprocess_usage(tmp_path: Path) -> None:
    _write(
        tmp_path / "src/meridian/lib/ops/neutral_module.py",
        """import os
import subprocess
import sys

def read_inputs() -> tuple[str | None, list[str]]:
    env_value = os.environ.get("MERIDIAN_HOME")
    args = list(sys.argv)
    subprocess.run(["echo", "ok"], check=False)
    return env_value, args
""",
    )

    assert architecture_checks._check_platform_boundary_drift(tmp_path) == []
