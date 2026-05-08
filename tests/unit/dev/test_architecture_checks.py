"""Tests for the architecture-check lane entrypoint."""

from __future__ import annotations

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
