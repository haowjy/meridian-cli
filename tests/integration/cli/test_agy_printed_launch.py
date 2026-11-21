"""A printed launch (``meridian agy``) writes nothing but its spill script."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import meridian.cli.main as cli_main
from meridian.lib.core.types import HarnessId
from tests.support.launch import stub_bundle_request_and_resolve


def _files(*roots: Path) -> set[Path]:
    return {path for root in roots for path in root.rglob("*") if path.is_file()}


def test_printed_launch_writes_nothing_but_its_script(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "mars.toml").write_text('[settings]\ntargets = [".agents"]\n', encoding="utf-8")
    monkeypatch.chdir(project)
    monkeypatch.setenv("MERIDIAN_PROJECT_DIR", project.as_posix())
    stub_bundle_request_and_resolve(monkeypatch, model="gemini-3.8-flash", harness=HarnessId.AGY)
    home = Path(os.environ["MERIDIAN_HOME"])
    before = _files(home, project)

    with pytest.raises(SystemExit) as exited:
        cli_main.main(argv=["agy", "-m", "gemini-3.8-flash"])

    assert exited.value.code == 0

    out = capsys.readouterr().out.strip()
    assert out.startswith(("(cd ", "sh ")) and "agy" in out
    new = _files(home, project) - before
    assert all(path.suffix == ".sh" and "launch" in path.parts for path in new), new
