"""Foreground spawn execution output/report boundary tests."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import meridian.lib.ops.spawn.execute as execute_module
from meridian.lib.config.settings import load_config
from meridian.lib.launch.request import SpawnRequest
from meridian.lib.ops.runtime import (
    build_runtime_from_root_and_config,
    resolve_runtime_authority_for_write,
)
from meridian.lib.ops.spawn.models import SpawnCreateInput
from meridian.lib.state import spawn_store

if TYPE_CHECKING:
    import pytest

# qa-validated: spawn-return-report


def test_execute_spawn_blocking_reads_report_and_does_not_print_running_preamble(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    monkeypatch.setenv("MERIDIAN_HOME", (tmp_path / "home").as_posix())
    authority = resolve_runtime_authority_for_write(project_root)
    assert authority.runtime_root is not None
    config = load_config(project_root, authority=authority)
    runtime = build_runtime_from_root_and_config(
        project_root,
        config,
        authority=authority,
    )

    async def _fake_launch_prepared_spawn(**kwargs: object) -> int:
        spawn = cast("Any", kwargs["spawn"])
        runtime_root = Path(cast("Path", kwargs["runtime_root"]))
        spawn_id = str(spawn.spawn_id)
        report_path = runtime_root / "spawns" / spawn_id / "report.md"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text("fake report body\n", encoding="utf-8")
        spawn_store.finalize_spawn(
            runtime_root,
            spawn_id,
            "succeeded",
            0,
            origin="runner",
            duration_secs=1.25,
        )
        return 0

    monkeypatch.setattr(execute_module, "launch_prepared_spawn", _fake_launch_prepared_spawn)

    result = execute_module.execute_spawn_blocking(
        payload=SpawnCreateInput(prompt="run"),
        request=SpawnRequest(
            prompt="run",
            model="gpt-5.4",
            harness="codex",
            agent="coder",
        ),
        runtime=runtime,
    )

    captured = capsys.readouterr()
    assert '{"spawn_id":' not in captured.out
    assert '"status": "running"' not in captured.out
    assert result.status == "succeeded"
    assert result.exit_code == 0
    assert result.report == "fake report body"
    assert result.duration_secs == 1.25
    assert result.format_text().endswith(
        "fake report body\n\nTranscript: meridian session log " + str(result.spawn_id)
    )
