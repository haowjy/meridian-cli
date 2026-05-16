import importlib
import io
import json
from typing import Any

import pytest

from meridian.lib.ops.spawn.models import (
    SpawnActionOutput,
    SpawnCreateInput,
    SpawnDetailOutput,
    SpawnForkInput,
    SpawnListEntry,
    SpawnListInput,
    SpawnListOutput,
    SpawnWaitInput,
    SpawnWaitMultiOutput,
)

cli_main = importlib.import_module("meridian.cli.main")
spawn_cli = importlib.import_module("meridian.cli.spawn")


class _FakeStdin(io.StringIO):
    def __init__(self, text: str, *, is_tty: bool) -> None:
        super().__init__(text)
        self._is_tty = is_tty

    def isatty(self) -> bool:
        return self._is_tty


def _wait_detail(
    *,
    spawn_id: str = "p123",
    report_body: str | None = "done report",
) -> SpawnDetailOutput:
    return SpawnDetailOutput(
        spawn_id=spawn_id,
        status="succeeded",
        model="gpt-5.4",
        harness="codex",
        started_at="2026-05-15T00:00:00Z",
        finished_at="2026-05-15T00:00:04Z",
        duration_secs=4.1,
        exit_code=0,
        failure_reason=None,
        input_tokens=120,
        output_tokens=80,
        cost_usd=0.0127,
        cost_is_estimate=True,
        report_path="/tmp/report.md",
        report_summary=report_body,
        report_body=report_body,
    )


def test_spawn_prompt_file_dash_reads_stdin_through_main(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MERIDIAN_DEPTH", "1")
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
    monkeypatch.setenv("MERIDIAN_DEPTH", "1")
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
    monkeypatch.setenv("MERIDIAN_DEPTH", "1")
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
    monkeypatch.setenv("MERIDIAN_DEPTH", "1")
    monkeypatch.setattr(spawn_cli.sys, "stdin", _FakeStdin("", is_tty=True))

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main(["--human", "spawn", "-p", "literal", "--goal", "   ", "--dry-run"])

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "error: --goal cannot be empty\n"


def test_spawn_list_rejects_agent_filter_spelling(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("MERIDIAN_DEPTH", "1")

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main(["--human", "spawn", "list", "--agent", "reviewer"])

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert 'Unknown option: "--agent"' in captured.err
    assert 'Unknown option: "-a"' not in captured.err


def test_spawn_list_recent_view_uses_active_statuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MERIDIAN_DEPTH", "1")
    captured: dict[str, object] = {}

    def _fake_spawn_list_sync(
        payload: SpawnListInput,
        *,
        sink: object | None = None,
        prepared: Any | None = None,
    ) -> SpawnListOutput:
        _ = (sink, prepared)
        captured["status"] = payload.status
        captured["statuses"] = payload.statuses
        return SpawnListOutput(spawns=())

    monkeypatch.setattr(spawn_cli, "spawn_list_sync", _fake_spawn_list_sync)

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main(["spawn", "list", "--view", "recent"])

    assert exc_info.value.code == 0
    assert captured["status"] is None
    assert captured["statuses"] == spawn_cli._ACTIVE_VIEW_STATUSES


def test_spawn_list_view_error_lists_recent_as_supported(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("MERIDIAN_DEPTH", "1")

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main(["--human", "spawn", "list", "--view", "not-a-view"])

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "Unsupported spawn view 'not-a-view'" in captured.err
    assert (
        "Supported views: active, recent, all, running, queued, completed, failed, cancelled"
        in captured.err
    )


def test_spawn_list_help_mentions_recent_active_view(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("MERIDIAN_DEPTH", "1")

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main(["spawn", "list", "--help"])

    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    rendered = f"{captured.out}\n{captured.err}"
    assert "recent (recent active spawns)" in rendered


def test_spawn_agent_launch_flag_still_targets_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MERIDIAN_DEPTH", "1")
    monkeypatch.setattr(spawn_cli.sys, "stdin", _FakeStdin("", is_tty=True))
    captured: dict[str, object] = {}

    def _fake_spawn_create_sync(
        payload: SpawnCreateInput,
        *,
        sink: object | None = None,
        prepared: Any | None = None,
    ) -> SpawnActionOutput:
        _ = (sink, prepared)
        captured["agent"] = payload.agent
        return SpawnActionOutput(command="spawn.create", status="dry-run")

    monkeypatch.setattr(spawn_cli, "spawn_create_sync", _fake_spawn_create_sync)

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main(["spawn", "--agent", "reviewer", "-p", "task", "--dry-run"])

    assert exc_info.value.code == 0
    assert captured["agent"] == "reviewer"


def test_spawn_runtime_error_is_reported_without_traceback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("MERIDIAN_DEPTH", "1")

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


def test_spawn_dry_run_text_includes_goal_contract_preview(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("MERIDIAN_DEPTH", "1")
    monkeypatch.setattr(spawn_cli.sys, "stdin", _FakeStdin("", is_tty=True))

    def _fake_spawn_create_sync(
        payload: SpawnCreateInput,
        *,
        sink: object | None = None,
        prepared: Any | None = None,
    ) -> SpawnActionOutput:
        _ = (payload, sink, prepared)
        return SpawnActionOutput(
            command="spawn.create",
            status="dry-run",
            message="Dry run complete.",
            goal="ship phase 3",
        )

    monkeypatch.setattr(spawn_cli, "spawn_create_sync", _fake_spawn_create_sync)

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main(["--human", "spawn", "-p", "literal", "--goal", "ship", "--dry-run"])

    assert exc_info.value.code == 0
    output = capsys.readouterr().out
    assert "Goal: ship phase 3" in output
    assert "Completion contract preview:" in output
    assert "# Spawn Goal" in output


def test_spawn_background_agent_mode_returns_wire_without_event_noise(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("MERIDIAN_DEPTH", "1")
    monkeypatch.setattr(spawn_cli.sys, "stdin", _FakeStdin("", is_tty=True))

    def _fake_spawn_create_sync(
        payload: SpawnCreateInput,
        *,
        sink: Any | None = None,
        prepared: Any | None = None,
    ) -> SpawnActionOutput:
        _ = prepared
        assert payload.background is True
        assert sink is not None
        sink.event({"t": "meridian.spawn.start", "id": "p123", "model": "gpt-5.4"})
        return SpawnActionOutput(
            command="spawn.create",
            status="running",
            spawn_id="p123",
            warning="heads up",
            model="gpt-5.4",
            harness_id="codex",
            agent="coder",
            context_from_resolved=("c7",),
            background=True,
        )

    monkeypatch.setattr(spawn_cli, "spawn_create_sync", _fake_spawn_create_sync)

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main(["spawn", "-p", "build", "--bg"])

    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert payload["status"] == "running"
    assert payload["spawn_id"] == "p123"
    assert "After spawning all subagents, you MUST run:" in payload["note"]
    assert "  meridian spawn wait" in payload["note"]


def test_spawn_background_explicit_json_preserves_rich_wire_and_events(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("MERIDIAN_DEPTH", "1")
    monkeypatch.setattr(spawn_cli.sys, "stdin", _FakeStdin("", is_tty=True))

    def _fake_spawn_create_sync(
        payload: SpawnCreateInput,
        *,
        sink: Any | None = None,
        prepared: Any | None = None,
    ) -> SpawnActionOutput:
        _ = prepared
        assert payload.background is True
        assert sink is not None
        sink.event({"t": "meridian.spawn.start", "id": "p456", "model": "gpt-5.4"})
        return SpawnActionOutput(
            command="spawn.create",
            status="running",
            spawn_id="p456",
            warning="explicit warning",
            context_from_resolved=("c9",),
            background=True,
        )

    monkeypatch.setattr(spawn_cli, "spawn_create_sync", _fake_spawn_create_sync)

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main(["--format", "json", "spawn", "-p", "build", "--bg"])

    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["status"] == "running"
    assert payload["spawn_id"] == "p456"
    assert payload["warning"] == "explicit warning"
    assert payload["context_from_resolved"] == ["c9"]
    assert captured.err.strip().startswith("{"), captured.err
    assert '"t":"meridian.spawn.start"' in captured.err


def test_spawn_background_human_text_submission_stays_unchanged(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("MERIDIAN_DEPTH", "1")
    monkeypatch.setattr(spawn_cli.sys, "stdin", _FakeStdin("", is_tty=True))

    def _fake_spawn_create_sync(
        payload: SpawnCreateInput,
        *,
        sink: Any | None = None,
        prepared: Any | None = None,
    ) -> SpawnActionOutput:
        _ = (sink, prepared)
        assert payload.background is True
        return SpawnActionOutput(
            command="spawn.create",
            status="running",
            spawn_id="p789",
            message="Background spawn submitted.",
            background=True,
            model="gpt-5.4",
            harness_id="codex",
        )

    monkeypatch.setattr(spawn_cli, "spawn_create_sync", _fake_spawn_create_sync)

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main(["--human", "spawn", "-p", "build", "--bg"])

    assert exc_info.value.code == 0
    rendered = capsys.readouterr().out
    assert rendered.startswith("Background spawn submitted.\nSpawn id: p789")
    assert "After spawning all subagents, you MUST run:" in rendered
    assert "Or wait for this spawn only: meridian spawn wait p789" in rendered
    assert "Transcript: meridian session log p789" not in rendered


def test_spawn_background_metadata_text_submission_stays_unchanged(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("MERIDIAN_DEPTH", "1")
    monkeypatch.setattr(spawn_cli.sys, "stdin", _FakeStdin("", is_tty=True))
    captured: dict[str, object] = {}

    def _fake_spawn_create_sync(
        payload: SpawnCreateInput,
        *,
        sink: Any | None = None,
        prepared: Any | None = None,
    ) -> SpawnActionOutput:
        _ = (sink, prepared)
        captured["verbose"] = payload.verbose
        assert payload.background is True
        return SpawnActionOutput(
            command="spawn.create",
            status="running",
            spawn_id="p790",
            message="Background spawn submitted.",
            background=True,
            model="gpt-5.4",
            harness_id="codex",
        )

    monkeypatch.setattr(spawn_cli, "spawn_create_sync", _fake_spawn_create_sync)

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main(["--human", "spawn", "-p", "build", "--bg", "--metadata"])

    assert exc_info.value.code == 0
    assert captured["verbose"] is False
    rendered = capsys.readouterr().out
    assert rendered.startswith("Background spawn submitted.\nSpawn id: p790")
    assert "After spawning all subagents, you MUST run:" in rendered
    assert "Or wait for this spawn only: meridian spawn wait p790" in rendered
    assert "Transcript: meridian session log p790" not in rendered
    assert "Report:" not in rendered
    assert "Input tokens:" not in rendered


def test_spawn_background_metadata_agent_mode_keeps_json_wire(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("MERIDIAN_DEPTH", "1")
    monkeypatch.setattr(spawn_cli.sys, "stdin", _FakeStdin("", is_tty=True))
    captured: dict[str, object] = {}

    def _fake_spawn_create_sync(
        payload: SpawnCreateInput,
        *,
        sink: Any | None = None,
        prepared: Any | None = None,
    ) -> SpawnActionOutput:
        _ = (sink, prepared)
        captured["verbose"] = payload.verbose
        assert payload.background is True
        return SpawnActionOutput(
            command="spawn.create",
            status="running",
            spawn_id="p791",
            warning="heads up",
            background=True,
        )

    monkeypatch.setattr(spawn_cli, "spawn_create_sync", _fake_spawn_create_sync)

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main(["spawn", "-p", "build", "--bg", "--metadata"])

    assert exc_info.value.code == 0
    assert captured["verbose"] is False
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "running"
    assert payload["spawn_id"] == "p791"
    assert payload["wait_required"] is True
    assert payload["wait_command"] == "meridian spawn wait"
    assert "transcript_command" not in payload


def test_spawn_children_agent_mode_uses_children_text_view(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("MERIDIAN_DEPTH", "1")
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


def test_spawn_wait_defaults_report_body_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MERIDIAN_DEPTH", "1")
    captured: dict[str, object] = {}

    def _fake_spawn_wait_sync(
        payload: SpawnWaitInput,
        *,
        sink: Any | None = None,
        prepared: Any | None = None,
    ) -> SpawnWaitMultiOutput:
        _ = (sink, prepared)
        captured["include_report_body"] = payload.include_report_body
        return SpawnWaitMultiOutput(
            spawns=(),
            total_runs=0,
            succeeded_runs=0,
            failed_runs=0,
            cancelled_runs=0,
            any_failed=False,
        )

    monkeypatch.setattr(spawn_cli, "spawn_wait_sync", _fake_spawn_wait_sync)

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main(["spawn", "wait"])

    assert exc_info.value.code == 0
    assert captured["include_report_body"] is True


def test_spawn_wait_no_report_disables_report_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MERIDIAN_DEPTH", "1")
    captured: dict[str, object] = {}

    def _fake_spawn_wait_sync(
        payload: SpawnWaitInput,
        *,
        sink: Any | None = None,
        prepared: Any | None = None,
    ) -> SpawnWaitMultiOutput:
        _ = (sink, prepared)
        captured["include_report_body"] = payload.include_report_body
        return SpawnWaitMultiOutput(
            spawns=(),
            total_runs=0,
            succeeded_runs=0,
            failed_runs=0,
            cancelled_runs=0,
            any_failed=False,
        )

    monkeypatch.setattr(spawn_cli, "spawn_wait_sync", _fake_spawn_wait_sync)

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main(["spawn", "wait", "--no-report"])

    assert exc_info.value.code == 0
    assert captured["include_report_body"] is False


def test_spawn_wait_single_agent_mode_default_text_is_compact_with_report(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("MERIDIAN_DEPTH", "1")

    def _fake_spawn_wait_sync(
        payload: SpawnWaitInput,
        *,
        sink: Any | None = None,
        prepared: Any | None = None,
    ) -> SpawnWaitMultiOutput:
        _ = (payload, sink, prepared)
        detail = _wait_detail(report_body="final report body")
        return SpawnWaitMultiOutput(
            spawns=(detail,),
            total_runs=1,
            succeeded_runs=1,
            failed_runs=0,
            cancelled_runs=0,
            any_failed=False,
            spawn_id=detail.spawn_id,
            status=detail.status,
            exit_code=detail.exit_code,
        )

    monkeypatch.setattr(spawn_cli, "spawn_wait_sync", _fake_spawn_wait_sync)

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main(["spawn", "wait", "p123"])

    assert exc_info.value.code == 0
    rendered = capsys.readouterr().out
    assert rendered.startswith("p123 succeeded (4.1s)\n\nfinal report body\n")
    assert "Transcript: meridian session log p123" in rendered
    assert "Spawn: p123" not in rendered


def test_spawn_wait_single_default_text_with_missing_report_still_shows_transcript(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("MERIDIAN_DEPTH", "1")

    def _fake_spawn_wait_sync(
        payload: SpawnWaitInput,
        *,
        sink: Any | None = None,
        prepared: Any | None = None,
    ) -> SpawnWaitMultiOutput:
        _ = (payload, sink, prepared)
        detail = _wait_detail(report_body=None)
        return SpawnWaitMultiOutput(
            spawns=(detail,),
            total_runs=1,
            succeeded_runs=1,
            failed_runs=0,
            cancelled_runs=0,
            any_failed=False,
            spawn_id=detail.spawn_id,
            status=detail.status,
            exit_code=detail.exit_code,
        )

    monkeypatch.setattr(spawn_cli, "spawn_wait_sync", _fake_spawn_wait_sync)

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main(["--human", "spawn", "wait", "p123", "--no-report"])

    assert exc_info.value.code == 0
    rendered = capsys.readouterr().out
    assert rendered.startswith("p123 succeeded (4.1s)\n\n")
    assert "final report body" not in rendered
    assert "Transcript: meridian session log p123" in rendered


def test_spawn_wait_single_verbose_text_shows_metadata(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("MERIDIAN_DEPTH", "1")

    def _fake_spawn_wait_sync(
        payload: SpawnWaitInput,
        *,
        sink: Any | None = None,
        prepared: Any | None = None,
    ) -> SpawnWaitMultiOutput:
        _ = (payload, sink, prepared)
        detail = _wait_detail(report_body="verbose report body")
        return SpawnWaitMultiOutput(
            spawns=(detail,),
            total_runs=1,
            succeeded_runs=1,
            failed_runs=0,
            cancelled_runs=0,
            any_failed=False,
            spawn_id=detail.spawn_id,
            status=detail.status,
            exit_code=detail.exit_code,
        )

    monkeypatch.setattr(spawn_cli, "spawn_wait_sync", _fake_spawn_wait_sync)

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main(["--human", "spawn", "wait", "p123", "--verbose"])

    assert exc_info.value.code == 0
    rendered = capsys.readouterr().out
    assert "Spawn: p123" in rendered
    assert "Model: gpt-5.4 (codex)" in rendered
    assert "verbose report body" in rendered
    assert "Transcript: meridian session log p123" in rendered


def test_spawn_wait_single_metadata_text_shows_metadata_without_verbose(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("MERIDIAN_DEPTH", "1")

    def _fake_spawn_wait_sync(
        payload: SpawnWaitInput,
        *,
        sink: Any | None = None,
        prepared: Any | None = None,
    ) -> SpawnWaitMultiOutput:
        _ = (sink, prepared)
        assert payload.verbose is False
        detail = _wait_detail(report_body="metadata report body")
        return SpawnWaitMultiOutput(
            spawns=(detail,),
            total_runs=1,
            succeeded_runs=1,
            failed_runs=0,
            cancelled_runs=0,
            any_failed=False,
            spawn_id=detail.spawn_id,
            status=detail.status,
            exit_code=detail.exit_code,
        )

    monkeypatch.setattr(spawn_cli, "spawn_wait_sync", _fake_spawn_wait_sync)

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main(["--human", "spawn", "wait", "p123", "--metadata"])

    assert exc_info.value.code == 0
    rendered = capsys.readouterr().out
    assert "Spawn: p123" in rendered
    assert "Model: gpt-5.4 (codex)" in rendered
    assert "Status: succeeded (exit 0)" in rendered
    assert "Duration: 4.1s" in rendered
    assert "Input tokens: 120" in rendered
    assert "Output tokens: 80" in rendered
    assert "Cost: ~$0.0127" in rendered
    assert "Report: /tmp/report.md" in rendered
    assert "metadata report body" in rendered
    assert "Transcript: meridian session log p123" in rendered


def test_spawn_wait_single_metadata_no_report_keeps_metadata_and_transcript(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("MERIDIAN_DEPTH", "1")
    captured: dict[str, object] = {}

    def _fake_spawn_wait_sync(
        payload: SpawnWaitInput,
        *,
        sink: Any | None = None,
        prepared: Any | None = None,
    ) -> SpawnWaitMultiOutput:
        _ = (sink, prepared)
        captured["include_report_body"] = payload.include_report_body
        captured["verbose"] = payload.verbose
        detail = _wait_detail(report_body=None)
        return SpawnWaitMultiOutput(
            spawns=(detail,),
            total_runs=1,
            succeeded_runs=1,
            failed_runs=0,
            cancelled_runs=0,
            any_failed=False,
            spawn_id=detail.spawn_id,
            status=detail.status,
            exit_code=detail.exit_code,
        )

    monkeypatch.setattr(spawn_cli, "spawn_wait_sync", _fake_spawn_wait_sync)

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main(["--human", "spawn", "wait", "p123", "--metadata", "--no-report"])

    assert exc_info.value.code == 0
    assert captured == {"include_report_body": False, "verbose": False}
    rendered = capsys.readouterr().out
    assert "Spawn: p123" in rendered
    assert "Input tokens: 120" in rendered
    assert "Output tokens: 80" in rendered
    assert "Cost: ~$0.0127" in rendered
    assert "metadata report body" not in rendered
    assert "Transcript: meridian session log p123" in rendered


def test_spawn_wait_multi_text_stays_table_plus_reports(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("MERIDIAN_DEPTH", "1")

    def _fake_spawn_wait_sync(
        payload: SpawnWaitInput,
        *,
        sink: Any | None = None,
        prepared: Any | None = None,
    ) -> SpawnWaitMultiOutput:
        _ = (payload, sink, prepared)
        return SpawnWaitMultiOutput(
            spawns=(
                _wait_detail(spawn_id="p101", report_body="first report"),
                _wait_detail(spawn_id="p102", report_body="second report"),
            ),
            total_runs=2,
            succeeded_runs=2,
            failed_runs=0,
            cancelled_runs=0,
            any_failed=False,
        )

    monkeypatch.setattr(spawn_cli, "spawn_wait_sync", _fake_spawn_wait_sync)

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main(["--human", "spawn", "wait", "p101", "p102"])

    assert exc_info.value.code == 0
    rendered = capsys.readouterr().out
    assert rendered.startswith("spawn_id")
    assert "p101" in rendered
    assert "p102" in rendered
    assert "Report for p101\nfirst report" in rendered
    assert "Report for p102\nsecond report" in rendered
    assert "Transcript: meridian session log" not in rendered


def test_spawn_foreground_agent_mode_default_text_is_compact_report(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("MERIDIAN_DEPTH", "1")
    monkeypatch.setattr(spawn_cli.sys, "stdin", _FakeStdin("", is_tty=True))

    def _fake_spawn_create_sync(
        payload: SpawnCreateInput,
        *,
        sink: Any | None = None,
        prepared: Any | None = None,
    ) -> SpawnActionOutput:
        _ = (payload, sink, prepared)
        return SpawnActionOutput(
            command="spawn.create",
            status="succeeded",
            spawn_id="p999",
            model="gpt-5.4",
            harness_id="codex",
            report="foreground report",
            duration_secs=2.3,
            exit_code=0,
        )

    monkeypatch.setattr(spawn_cli, "spawn_create_sync", _fake_spawn_create_sync)

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main(["spawn", "-p", "ship it"])

    assert exc_info.value.code == 0
    rendered = capsys.readouterr().out
    assert rendered.startswith("p999 succeeded (2.3s)\n\nforeground report\n")
    assert "Transcript: meridian session log p999" in rendered
    assert "Spawn id: p999" not in rendered


def test_spawn_foreground_explicit_text_is_compact_report(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("MERIDIAN_DEPTH", "1")
    monkeypatch.setattr(spawn_cli.sys, "stdin", _FakeStdin("", is_tty=True))

    def _fake_spawn_create_sync(
        payload: SpawnCreateInput,
        *,
        sink: Any | None = None,
        prepared: Any | None = None,
    ) -> SpawnActionOutput:
        _ = (payload, sink, prepared)
        return SpawnActionOutput(
            command="spawn.create",
            status="succeeded",
            spawn_id="p997",
            report="explicit text report",
            duration_secs=1.8,
            exit_code=0,
        )

    monkeypatch.setattr(spawn_cli, "spawn_create_sync", _fake_spawn_create_sync)

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main(["--format", "text", "spawn", "-p", "ship it"])

    assert exc_info.value.code == 0
    rendered = capsys.readouterr().out
    assert rendered == (
        "p997 succeeded (1.8s)\n\n"
        "explicit text report\n\n"
        "Transcript: meridian session log p997\n"
    )


def test_spawn_foreground_default_text_no_report_still_shows_transcript(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("MERIDIAN_DEPTH", "1")
    monkeypatch.setattr(spawn_cli.sys, "stdin", _FakeStdin("", is_tty=True))

    def _fake_spawn_create_sync(
        payload: SpawnCreateInput,
        *,
        sink: Any | None = None,
        prepared: Any | None = None,
    ) -> SpawnActionOutput:
        _ = (payload, sink, prepared)
        return SpawnActionOutput(
            command="spawn.create",
            status="succeeded",
            spawn_id="p999",
            model="gpt-5.4",
            harness_id="codex",
            report=None,
            duration_secs=2.3,
            exit_code=0,
        )

    monkeypatch.setattr(spawn_cli, "spawn_create_sync", _fake_spawn_create_sync)

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main(["--human", "spawn", "-p", "ship it"])

    assert exc_info.value.code == 0
    rendered = capsys.readouterr().out
    assert "(no report)" in rendered
    assert "Transcript: meridian session log p999" in rendered


def test_spawn_foreground_metadata_text_shows_metadata_without_verbose(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("MERIDIAN_DEPTH", "1")
    monkeypatch.setattr(spawn_cli.sys, "stdin", _FakeStdin("", is_tty=True))
    captured: dict[str, object] = {}

    def _fake_spawn_create_sync(
        payload: SpawnCreateInput,
        *,
        sink: Any | None = None,
        prepared: Any | None = None,
    ) -> SpawnActionOutput:
        _ = (sink, prepared)
        captured["verbose"] = payload.verbose
        return SpawnActionOutput(
            command="spawn.create",
            status="succeeded",
            spawn_id="p999",
            model="gpt-5.4",
            harness_id="codex",
            report="foreground report",
            duration_secs=2.3,
            exit_code=0,
        )

    def _fake_spawn_show_sync(
        payload: Any,
        *,
        sink: Any | None = None,
        prepared: Any | None = None,
    ) -> SpawnDetailOutput:
        _ = (sink, prepared)
        captured["spawn_id"] = payload.spawn_id
        captured["include_report_body"] = payload.include_report_body
        return _wait_detail(spawn_id="p999", report_body="metadata foreground report")

    monkeypatch.setattr(spawn_cli, "spawn_create_sync", _fake_spawn_create_sync)
    monkeypatch.setattr(spawn_cli, "spawn_show_sync", _fake_spawn_show_sync)

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main(["--human", "spawn", "-p", "ship it", "--metadata"])

    assert exc_info.value.code == 0
    assert captured == {"verbose": False, "spawn_id": "p999", "include_report_body": True}
    rendered = capsys.readouterr().out
    assert "Spawn: p999" in rendered
    assert "Model: gpt-5.4 (codex)" in rendered
    assert "Status: succeeded (exit 0)" in rendered
    assert "Duration: 4.1s" in rendered
    assert "Input tokens: 120" in rendered
    assert "Output tokens: 80" in rendered
    assert "Cost: ~$0.0127" in rendered
    assert "Report: /tmp/report.md" in rendered
    assert "metadata foreground report" in rendered
    assert "Transcript: meridian session log p999" in rendered


def test_spawn_foreground_metadata_falls_back_without_spawn_show(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("MERIDIAN_DEPTH", "1")
    monkeypatch.setattr(spawn_cli.sys, "stdin", _FakeStdin("", is_tty=True))

    def _fake_spawn_create_sync(
        payload: SpawnCreateInput,
        *,
        sink: Any | None = None,
        prepared: Any | None = None,
    ) -> SpawnActionOutput:
        _ = (payload, sink, prepared)
        return SpawnActionOutput(
            command="spawn.create",
            status="succeeded",
            spawn_id="p996",
            model="gpt-5.4",
            harness_id="codex",
            report="fallback report",
            duration_secs=2.3,
            exit_code=0,
        )

    def _fail_spawn_show_sync(*_args: Any, **_kwargs: Any) -> SpawnDetailOutput:
        raise KeyError("missing spawn")

    monkeypatch.setattr(spawn_cli, "spawn_create_sync", _fake_spawn_create_sync)
    monkeypatch.setattr(spawn_cli, "spawn_show_sync", _fail_spawn_show_sync)

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main(["--human", "spawn", "-p", "ship it", "--metadata"])

    assert exc_info.value.code == 0
    rendered = capsys.readouterr().out
    assert "Spawn id: p996" in rendered
    assert "Model: gpt-5.4 (codex)" in rendered
    assert "Exit code: 0" in rendered
    assert "fallback report" in rendered
    assert "Transcript: meridian session log p996" in rendered


def test_spawn_foreground_verbose_text_shows_metadata(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("MERIDIAN_DEPTH", "1")
    monkeypatch.setattr(spawn_cli.sys, "stdin", _FakeStdin("", is_tty=True))

    def _fake_spawn_create_sync(
        payload: SpawnCreateInput,
        *,
        sink: Any | None = None,
        prepared: Any | None = None,
    ) -> SpawnActionOutput:
        _ = (payload, sink, prepared)
        return SpawnActionOutput(
            command="spawn.create",
            status="succeeded",
            spawn_id="p999",
            model="gpt-5.4",
            harness_id="codex",
            report="foreground report",
            duration_secs=2.3,
            exit_code=0,
        )

    monkeypatch.setattr(spawn_cli, "spawn_create_sync", _fake_spawn_create_sync)

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main(["--human", "spawn", "-p", "ship it", "--verbose"])

    assert exc_info.value.code == 0
    rendered = capsys.readouterr().out
    assert "Spawn id: p999" in rendered
    assert "Model: gpt-5.4 (codex)" in rendered
    assert "Exit code: 0" in rendered
    assert "foreground report" in rendered
    assert "Transcript: meridian session log p999" in rendered


def test_spawn_explicit_json_includes_report_and_transcript_command(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("MERIDIAN_DEPTH", "1")
    monkeypatch.setattr(spawn_cli.sys, "stdin", _FakeStdin("", is_tty=True))

    def _fake_spawn_create_sync(
        payload: SpawnCreateInput,
        *,
        sink: Any | None = None,
        prepared: Any | None = None,
    ) -> SpawnActionOutput:
        _ = (payload, sink, prepared)
        return SpawnActionOutput(
            command="spawn.create",
            status="succeeded",
            spawn_id="p999",
            model="gpt-5.4",
            harness_id="codex",
            report="foreground report",
            duration_secs=2.3,
            exit_code=0,
        )

    monkeypatch.setattr(spawn_cli, "spawn_create_sync", _fake_spawn_create_sync)

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main(["--format", "json", "spawn", "-p", "ship it"])

    assert exc_info.value.code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "succeeded"
    assert payload["spawn_id"] == "p999"
    assert payload["report"] == "foreground report"
    assert payload["transcript_command"] == "meridian session log p999"


def test_spawn_explicit_json_metadata_stays_json(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("MERIDIAN_DEPTH", "1")
    monkeypatch.setattr(spawn_cli.sys, "stdin", _FakeStdin("", is_tty=True))
    captured: dict[str, object] = {}

    def _fake_spawn_create_sync(
        payload: SpawnCreateInput,
        *,
        sink: Any | None = None,
        prepared: Any | None = None,
    ) -> SpawnActionOutput:
        _ = (sink, prepared)
        captured["verbose"] = payload.verbose
        return SpawnActionOutput(
            command="spawn.create",
            status="succeeded",
            spawn_id="p995",
            report="foreground report",
            duration_secs=2.3,
            exit_code=0,
        )

    monkeypatch.setattr(spawn_cli, "spawn_create_sync", _fake_spawn_create_sync)

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main(["--format", "json", "spawn", "-p", "ship it", "--metadata"])

    assert exc_info.value.code == 0
    assert captured["verbose"] is False
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "succeeded"
    assert payload["spawn_id"] == "p995"
    assert payload["report"] == "foreground report"
    assert payload["transcript_command"] == "meridian session log p995"


def test_spawn_wait_explicit_json_includes_report_and_transcript_command(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("MERIDIAN_DEPTH", "1")

    def _fake_spawn_wait_sync(
        payload: SpawnWaitInput,
        *,
        sink: Any | None = None,
        prepared: Any | None = None,
    ) -> SpawnWaitMultiOutput:
        _ = (payload, sink, prepared)
        detail = _wait_detail(report_body="final report body")
        return SpawnWaitMultiOutput(
            spawns=(detail,),
            total_runs=1,
            succeeded_runs=1,
            failed_runs=0,
            cancelled_runs=0,
            any_failed=False,
            spawn_id=detail.spawn_id,
            status=detail.status,
            exit_code=detail.exit_code,
        )

    monkeypatch.setattr(spawn_cli, "spawn_wait_sync", _fake_spawn_wait_sync)

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main(["--format", "json", "spawn", "wait", "p123"])

    assert exc_info.value.code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["spawn_id"] == "p123"
    assert payload["status"] == "succeeded"
    assert payload["report_body"] == "final report body"
    assert payload["transcript_command"] == "meridian session log p123"
    assert payload["spawns"][0]["report_body"] == "final report body"
    assert payload["spawns"][0]["transcript_command"] == "meridian session log p123"


def test_spawn_wait_explicit_text_is_compact_report(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("MERIDIAN_DEPTH", "1")

    def _fake_spawn_wait_sync(
        payload: SpawnWaitInput,
        *,
        sink: Any | None = None,
        prepared: Any | None = None,
    ) -> SpawnWaitMultiOutput:
        _ = (payload, sink, prepared)
        detail = _wait_detail(report_body="explicit wait text")
        return SpawnWaitMultiOutput(
            spawns=(detail,),
            total_runs=1,
            succeeded_runs=1,
            failed_runs=0,
            cancelled_runs=0,
            any_failed=False,
            spawn_id=detail.spawn_id,
            status=detail.status,
            exit_code=detail.exit_code,
        )

    monkeypatch.setattr(spawn_cli, "spawn_wait_sync", _fake_spawn_wait_sync)

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main(["--format", "text", "spawn", "wait", "p123"])

    assert exc_info.value.code == 0
    rendered = capsys.readouterr().out
    assert rendered == (
        "p123 succeeded (4.1s)\n\n"
        "explicit wait text\n\n"
        "Transcript: meridian session log p123\n"
    )


def test_spawn_wait_explicit_json_no_report_keeps_transcript_without_body(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("MERIDIAN_DEPTH", "1")
    captured: dict[str, object] = {}

    def _fake_spawn_wait_sync(
        payload: SpawnWaitInput,
        *,
        sink: Any | None = None,
        prepared: Any | None = None,
    ) -> SpawnWaitMultiOutput:
        _ = (sink, prepared)
        captured["include_report_body"] = payload.include_report_body
        detail = _wait_detail(report_body=None)
        return SpawnWaitMultiOutput(
            spawns=(detail,),
            total_runs=1,
            succeeded_runs=1,
            failed_runs=0,
            cancelled_runs=0,
            any_failed=False,
            spawn_id=detail.spawn_id,
            status=detail.status,
            exit_code=detail.exit_code,
        )

    monkeypatch.setattr(spawn_cli, "spawn_wait_sync", _fake_spawn_wait_sync)

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main(["--format", "json", "spawn", "wait", "p123", "--no-report"])

    assert exc_info.value.code == 0
    assert captured["include_report_body"] is False
    payload = json.loads(capsys.readouterr().out)
    assert payload["spawn_id"] == "p123"
    assert payload["transcript_command"] == "meridian session log p123"
    assert "report_body" not in payload
    assert "report_body" not in payload["spawns"][0]


def test_spawn_wait_explicit_json_metadata_stays_json(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("MERIDIAN_DEPTH", "1")
    captured: dict[str, object] = {}

    def _fake_spawn_wait_sync(
        payload: SpawnWaitInput,
        *,
        sink: Any | None = None,
        prepared: Any | None = None,
    ) -> SpawnWaitMultiOutput:
        _ = (sink, prepared)
        captured["verbose"] = payload.verbose
        detail = _wait_detail(report_body="json metadata wait")
        return SpawnWaitMultiOutput(
            spawns=(detail,),
            total_runs=1,
            succeeded_runs=1,
            failed_runs=0,
            cancelled_runs=0,
            any_failed=False,
            spawn_id=detail.spawn_id,
            status=detail.status,
            exit_code=detail.exit_code,
        )

    monkeypatch.setattr(spawn_cli, "spawn_wait_sync", _fake_spawn_wait_sync)

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main(["--format", "json", "spawn", "wait", "p123", "--metadata"])

    assert exc_info.value.code == 0
    assert captured["verbose"] is False
    payload = json.loads(capsys.readouterr().out)
    assert payload["spawn_id"] == "p123"
    assert payload["report_body"] == "json metadata wait"
    assert payload["transcript_command"] == "meridian session log p123"


def test_spawn_wait_explicit_json_multi_has_per_spawn_reports_without_single_top_level(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("MERIDIAN_DEPTH", "1")

    def _fake_spawn_wait_sync(
        payload: SpawnWaitInput,
        *,
        sink: Any | None = None,
        prepared: Any | None = None,
    ) -> SpawnWaitMultiOutput:
        _ = (payload, sink, prepared)
        return SpawnWaitMultiOutput(
            spawns=(
                _wait_detail(spawn_id="p101", report_body="first report"),
                _wait_detail(spawn_id="p102", report_body="second report"),
            ),
            total_runs=2,
            succeeded_runs=2,
            failed_runs=0,
            cancelled_runs=0,
            any_failed=False,
        )

    monkeypatch.setattr(spawn_cli, "spawn_wait_sync", _fake_spawn_wait_sync)

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main(["--format", "json", "spawn", "wait", "p101", "p102"])

    assert exc_info.value.code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["total_runs"] == 2
    assert "report_body" not in payload
    assert "transcript_command" not in payload
    assert payload["spawns"][0]["report_body"] == "first report"
    assert payload["spawns"][0]["transcript_command"] == "meridian session log p101"
    assert payload["spawns"][1]["report_body"] == "second report"
    assert payload["spawns"][1]["transcript_command"] == "meridian session log p102"


def test_spawn_show_human_text_remains_detail_view(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("MERIDIAN_DEPTH", "1")

    def _fake_spawn_show_sync(
        payload: Any,
        *,
        sink: Any | None = None,
        prepared: Any | None = None,
    ) -> SpawnDetailOutput:
        _ = (payload, sink, prepared)
        return _wait_detail(spawn_id="p123", report_body="show report body")

    monkeypatch.setattr(spawn_cli, "spawn_show_sync", _fake_spawn_show_sync)

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main(["--human", "spawn", "show", "p123"])

    assert exc_info.value.code == 0
    rendered = capsys.readouterr().out
    assert rendered.startswith("Spawn: p123")
    assert "Model: gpt-5.4 (codex)" in rendered
    assert "Report: /tmp/report.md" in rendered
    assert "show report body" in rendered
    assert not rendered.startswith("p123 succeeded")


def test_spawn_show_explicit_json_remains_detail_view(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("MERIDIAN_DEPTH", "1")

    def _fake_spawn_show_sync(
        payload: Any,
        *,
        sink: Any | None = None,
        prepared: Any | None = None,
    ) -> SpawnDetailOutput:
        _ = (payload, sink, prepared)
        return _wait_detail(spawn_id="p123", report_body="show report body")

    monkeypatch.setattr(spawn_cli, "spawn_show_sync", _fake_spawn_show_sync)

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main(["--format", "json", "spawn", "show", "p123"])

    assert exc_info.value.code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["spawn_id"] == "p123"
    assert payload["model"] == "gpt-5.4"
    assert payload["harness"] == "codex"
    assert payload["report_path"] == "/tmp/report.md"
    assert payload["report_body"] == "show report body"
    assert "transcript_command" not in payload


def test_spawn_bare_fork_uses_meridian_spawn_id_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MERIDIAN_DEPTH", "1")
    monkeypatch.setenv("MERIDIAN_SPAWN_ID", "p321")
    monkeypatch.setattr(spawn_cli.sys, "stdin", _FakeStdin("", is_tty=True))
    captured: dict[str, object] = {}

    def _fake_spawn_fork_sync(
        payload: SpawnForkInput,
        *,
        sink: object | None = None,
        prepared: Any | None = None,
    ) -> SpawnActionOutput:
        _ = (sink, prepared)
        captured["source_ref"] = payload.source_ref
        captured["model"] = payload.model
        captured["agent"] = payload.agent
        captured["skills"] = payload.skills
        captured["inherit_source_skills"] = payload.inherit_source_skills
        return SpawnActionOutput(command="spawn.fork", status="dry-run")

    monkeypatch.setattr(spawn_cli, "spawn_fork_sync", _fake_spawn_fork_sync)

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main(["spawn", "--fork", "-p", "branch", "--dry-run"])

    assert exc_info.value.code == 0
    assert captured == {
        "source_ref": "p321",
        "model": "",
        "agent": None,
        "skills": (),
        "inherit_source_skills": True,
    }


def test_spawn_bare_fork_without_meridian_spawn_id_errors(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("MERIDIAN_DEPTH", "1")
    monkeypatch.delenv("MERIDIAN_SPAWN_ID", raising=False)
    monkeypatch.setattr(spawn_cli.sys, "stdin", _FakeStdin("", is_tty=True))

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main(["--human", "spawn", "--fork", "-p", "branch", "--dry-run"])

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert (
        captured.err
        == "error: Cannot infer --fork target: not inside a Meridian-managed session. "
        "Pass --fork REF explicitly.\n"
    )


def test_spawn_bare_fork_with_continue_reports_conflict_before_inference(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("MERIDIAN_DEPTH", "1")
    monkeypatch.delenv("MERIDIAN_SPAWN_ID", raising=False)
    monkeypatch.setattr(spawn_cli.sys, "stdin", _FakeStdin("", is_tty=True))

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main(
            [
                "--human",
                "spawn",
                "--fork",
                "--continue",
                "p123",
                "-p",
                "branch",
                "--dry-run",
            ]
        )

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "error: Cannot combine --fork with --continue.\n"


def test_spawn_bare_fork_fresh_with_continue_reports_conflict_before_inference(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("MERIDIAN_DEPTH", "1")
    monkeypatch.delenv("MERIDIAN_SPAWN_ID", raising=False)
    monkeypatch.setattr(spawn_cli.sys, "stdin", _FakeStdin("", is_tty=True))

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main(
            [
                "--human",
                "spawn",
                "--fork-fresh",
                "--continue",
                "p123",
                "-p",
                "branch",
                "--dry-run",
            ]
        )

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "error: Cannot combine --fork-fresh with --continue.\n"


def test_spawn_bare_fork_with_from_reports_conflict_before_inference(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("MERIDIAN_DEPTH", "1")
    monkeypatch.delenv("MERIDIAN_SPAWN_ID", raising=False)
    monkeypatch.setattr(spawn_cli.sys, "stdin", _FakeStdin("", is_tty=True))

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main(
            [
                "--human",
                "spawn",
                "--fork",
                "--from",
                "p123",
                "-p",
                "branch",
                "--dry-run",
            ]
        )

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "error: Cannot combine --fork with --from (MVP limitation).\n"


def test_spawn_fork_rejects_identity_overrides(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("MERIDIAN_DEPTH", "1")
    monkeypatch.setattr(spawn_cli.sys, "stdin", _FakeStdin("", is_tty=True))

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main(
            ["--human", "spawn", "--fork", "p123", "-a", "reviewer", "-p", "branch", "--dry-run"]
        )

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert (
        captured.err
        == "error: --fork preserves launch identity. "
        "Use --fork-fresh to change agent, model, or skills.\n"
    )


def test_spawn_fork_fresh_accepts_identity_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MERIDIAN_DEPTH", "1")
    monkeypatch.setattr(spawn_cli.sys, "stdin", _FakeStdin("", is_tty=True))
    captured: dict[str, object] = {}

    def _fake_spawn_fork_sync(
        payload: SpawnForkInput,
        *,
        sink: object | None = None,
        prepared: Any | None = None,
    ) -> SpawnActionOutput:
        _ = (sink, prepared)
        captured["source_ref"] = payload.source_ref
        captured["model"] = payload.model
        captured["agent"] = payload.agent
        captured["skills"] = payload.skills
        captured["inherit_source_skills"] = payload.inherit_source_skills
        return SpawnActionOutput(command="spawn.fork", status="dry-run")

    monkeypatch.setattr(spawn_cli, "spawn_fork_sync", _fake_spawn_fork_sync)

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main(
            [
                "spawn",
                "--fork-fresh",
                "p123",
                "-a",
                "reviewer",
                "-m",
                "gpt-5.4-mini",
                "--skills",
                "skill-a",
                "-p",
                "branch",
                "--dry-run",
            ]
        )

    assert exc_info.value.code == 0
    assert captured == {
        "source_ref": "p123",
        "model": "gpt-5.4-mini",
        "agent": "reviewer",
        "skills": ("skill-a",),
        "inherit_source_skills": False,
    }


def test_spawn_rejects_combining_fork_and_fork_fresh(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("MERIDIAN_DEPTH", "1")
    monkeypatch.setattr(spawn_cli.sys, "stdin", _FakeStdin("", is_tty=True))

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main(
            [
                "--human",
                "spawn",
                "--fork",
                "p123",
                "--fork-fresh",
                "p123",
                "-p",
                "branch",
                "--dry-run",
            ]
        )

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "error: Cannot combine --fork with --fork-fresh.\n"


def test_spawn_rejects_fork_fresh_with_from(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("MERIDIAN_DEPTH", "1")
    monkeypatch.setattr(spawn_cli.sys, "stdin", _FakeStdin("", is_tty=True))

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main(
            [
                "--human",
                "spawn",
                "--fork-fresh",
                "p123",
                "--from",
                "p123",
                "-p",
                "branch",
                "--dry-run",
            ]
        )

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "error: Cannot combine --fork-fresh with --from (MVP limitation).\n"
