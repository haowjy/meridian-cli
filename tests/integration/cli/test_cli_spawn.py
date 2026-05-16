import importlib
import io
import json
from typing import Any

import pytest

from meridian.lib.ops.spawn.models import (
    SpawnActionOutput,
    SpawnCreateInput,
    SpawnForkInput,
    SpawnListEntry,
    SpawnListInput,
    SpawnListOutput,
)

cli_main = importlib.import_module("meridian.cli.main")
spawn_cli = importlib.import_module("meridian.cli.spawn")


class _FakeStdin(io.StringIO):
    def __init__(self, text: str, *, is_tty: bool) -> None:
        super().__init__(text)
        self._is_tty = is_tty

    def isatty(self) -> bool:
        return self._is_tty


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
