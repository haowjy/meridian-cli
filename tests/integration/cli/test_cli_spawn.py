import importlib
import io
from pathlib import Path
from typing import Any

import pytest

from meridian.lib.ops.spawn.models import (
    SpawnActionOutput,
    SpawnCreateInput,
    SpawnDetailOutput,
    SpawnListEntry,
    SpawnListOutput,
    SpawnShowInput,
    SpawnStatusInput,
)

cli_main = importlib.import_module("meridian.cli.main")
spawn_cli = importlib.import_module("meridian.cli.spawn")


class _FakeStdin(io.StringIO):
    def __init__(self, text: str, *, is_tty: bool) -> None:
        super().__init__(text)
        self._is_tty = is_tty

    def isatty(self) -> bool:
        return self._is_tty


@pytest.fixture(autouse=True)
def _isolate_cli_main_invocations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MERIDIAN_DEPTH", "1")
    monkeypatch.chdir(tmp_path)
    (tmp_path / "meridian.toml").write_text("[defaults]\n", encoding="utf-8")
    monkeypatch.setenv("MERIDIAN_HOME", (tmp_path / "home").as_posix())
    monkeypatch.delenv("MERIDIAN_CONFIG", raising=False)


def test_spawn_prompt_file_dash_reads_stdin_through_main(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def _fake_spawn_create_sync(
        payload: SpawnCreateInput,
        *,
        sink: object | None = None,
        prepared: Any | None = None,
    ) -> SpawnActionOutput:
        _ = (sink, prepared)
        captured["prompt"] = payload.prompt
        return SpawnActionOutput(command="spawn.create", status="dry-run")

    monkeypatch.setattr(spawn_cli, "spawn_create_sync", _fake_spawn_create_sync)
    monkeypatch.setattr(spawn_cli.sys, "stdin", _FakeStdin("stdin prompt", is_tty=False))

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main(["spawn", "-a", "reviewer", "--prompt-file", "-", "--dry-run"])

    assert exc_info.value.code == 0
    assert captured["prompt"] == "stdin prompt"


def test_spawn_rejects_prompt_and_prompt_file_together(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(spawn_cli.sys, "stdin", _FakeStdin("", is_tty=True))

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main(["--human", "spawn", "-p", "literal", "--prompt-file", "-", "--dry-run"])

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "error: cannot specify both -p and --prompt-file\n"


def test_spawn_goal_is_trimmed_and_passed_to_spawn_create(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(spawn_cli.sys, "stdin", _FakeStdin("", is_tty=True))
    captured: dict[str, object] = {}

    def _fake_spawn_create_sync(
        payload: SpawnCreateInput,
        *,
        sink: object | None = None,
        prepared: Any | None = None,
    ) -> SpawnActionOutput:
        _ = (sink, prepared)
        captured["goal"] = payload.goal
        return SpawnActionOutput(command="spawn.create", status="dry-run")

    monkeypatch.setattr(spawn_cli, "spawn_create_sync", _fake_spawn_create_sync)

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main(["spawn", "-p", "literal", "--goal", "  ship phase 3  ", "--dry-run"])

    assert exc_info.value.code == 0
    assert captured["goal"] == "ship phase 3"


def test_spawn_goal_rejects_empty_value(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(spawn_cli.sys, "stdin", _FakeStdin("", is_tty=True))

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main(["--human", "spawn", "-p", "literal", "--goal", "   ", "--dry-run"])

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "error: --goal cannot be empty\n"


def test_spawn_continue_rejects_task_dir_override(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(spawn_cli.sys, "stdin", _FakeStdin("", is_tty=True))

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main(
            [
                "--human",
                "spawn",
                "--continue",
                "p123",
                "-p",
                "follow-up",
                "--task-dir",
                ".",
            ]
        )

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "--continue does not accept --task-dir" in captured.err


def test_spawn_runtime_error_is_reported_without_traceback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def _fail_spawn_create_sync(
        payload: SpawnCreateInput,
        *,
        sink: object | None = None,
        prepared: Any | None = None,
    ) -> SpawnActionOutput:
        _ = (payload, sink, prepared)
        raise RuntimeError(
            "Mars model resolution failed: no mars.toml found for this project root. "
            "Add mars.toml or choose a fully qualified model id."
        )

    monkeypatch.setattr(spawn_cli, "spawn_create_sync", _fail_spawn_create_sync)
    monkeypatch.setattr(spawn_cli.sys, "stdin", _FakeStdin("", is_tty=True))

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main(["spawn", "-a", "reviewer", "-p", "build", "--dry-run"])

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err
    assert "mars.toml" in captured.err


def test_spawn_children_agent_mode_uses_children_text_view(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output = SpawnListOutput(
        spawns=(
            SpawnListEntry(
                spawn_id="p101",
                status="succeeded",
                model="gpt-5.4",
                agent="reviewer",
                desc="review child",
                duration_secs=0.7,
                cost_usd=0.01,
            ),
        ),
        text_view="children",
    )
    monkeypatch.setattr(spawn_cli, "spawn_children_sync", lambda *_args, **_kwargs: output)

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main(["spawn", "children", "p100"])

    assert exc_info.value.code == 0
    rendered = capsys.readouterr().out
    assert "reviewer" in rendered
    assert "review child" in rendered


def _spawn_detail_output(report_body: str | None = "report body") -> SpawnDetailOutput:
    return SpawnDetailOutput(
        spawn_id="p500",
        status="succeeded",
        model="gpt-5.4",
        harness="codex",
        started_at="2026-05-15T00:00:00Z",
        finished_at="2026-05-15T00:00:01Z",
        duration_secs=1.0,
        exit_code=0,
        failure_reason=None,
        input_tokens=None,
        output_tokens=None,
        cost_usd=None,
        report_path="/tmp/report.md",
        report_summary=report_body,
        report_body=report_body,
    )


def test_spawn_show_and_status_report_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    show_include_report: dict[str, bool] = {}
    status_include_report: dict[str, bool] = {}

    def _fake_spawn_show_sync(
        payload: SpawnShowInput,
        *,
        sink: object | None = None,
        prepared: Any | None = None,
    ) -> SpawnDetailOutput:
        _ = (sink, prepared)
        show_include_report[payload.spawn_id] = payload.include_report_body
        return _spawn_detail_output("report body" if payload.include_report_body else None)

    def _fake_spawn_status_sync(
        payload: SpawnStatusInput,
        *,
        sink: object | None = None,
        prepared: Any | None = None,
    ) -> SpawnDetailOutput:
        _ = (sink, prepared)
        status_include_report[payload.spawn_id] = payload.include_report_body
        return _spawn_detail_output("report body" if payload.include_report_body else None)

    monkeypatch.setattr(spawn_cli, "spawn_show_sync", _fake_spawn_show_sync)
    monkeypatch.setattr(spawn_cli, "spawn_status_sync", _fake_spawn_status_sync)

    with pytest.raises(SystemExit) as show_exit:
        cli_main.main(["spawn", "show", "p-show"])
    with pytest.raises(SystemExit) as show_no_report_exit:
        cli_main.main(["spawn", "show", "--no-report", "p-show-no-report"])
    with pytest.raises(SystemExit) as status_exit:
        cli_main.main(["spawn", "status", "p-status"])
    with pytest.raises(SystemExit) as status_report_exit:
        cli_main.main(["spawn", "status", "--report", "p-status-report"])

    assert show_exit.value.code == 0
    assert show_no_report_exit.value.code == 0
    assert status_exit.value.code == 0
    assert status_report_exit.value.code == 0
    assert show_include_report["p-show"] is True
    assert show_include_report["p-show-no-report"] is False
    assert "p-status" not in show_include_report
    assert "p-status-report" not in show_include_report
    assert status_include_report["p-status"] is False
    assert status_include_report["p-status-report"] is True


def test_spawn_status_verbose_renders_internal_fields(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        spawn_cli,
        "spawn_status_sync",
        lambda *_args, **_kwargs: SpawnDetailOutput(
            spawn_id="p501",
            status="succeeded",
            model="gpt-5.4",
            harness="codex",
            started_at="2026-05-15T00:00:00Z",
            finished_at="2026-05-15T00:00:01Z",
            duration_secs=1.0,
            exit_code=0,
            failure_reason=None,
            input_tokens=123,
            output_tokens=456,
            cost_usd=0.1,
            report_path="/tmp/report.md",
            report_summary=None,
            report_body=None,
        ),
    )

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main(["--human", "spawn", "status", "p501", "--verbose"])

    assert exc_info.value.code == 0
    rendered = capsys.readouterr().out
    assert "Input tokens: 123" in rendered
