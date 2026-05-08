import importlib
import io
import json
from types import SimpleNamespace
from typing import Any

import pytest

from meridian.lib.ops.spawn.models import (
    SpawnActionOutput,
    SpawnContinueInput,
    SpawnCreateInput,
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


def test_spawn_file_only_without_prompt_is_allowed(
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
        captured["files"] = payload.files
        return SpawnActionOutput(command="spawn.create", status="dry-run")

    monkeypatch.setattr(spawn_cli, "spawn_create_sync", _fake_spawn_create_sync)
    monkeypatch.setattr(spawn_cli.sys, "stdin", _FakeStdin("", is_tty=False))

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main(["spawn", "--file", "README.md", "--dry-run"])

    assert exc_info.value.code == 0
    assert captured["prompt"] == ""
    assert captured["files"] == ("README.md",)


def test_spawn_continue_without_prompt_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MERIDIAN_DEPTH", "1")
    captured: dict[str, object] = {}

    def _fake_spawn_continue_sync(
        payload: SpawnContinueInput,
        *,
        sink: object | None = None,
        prepared: Any | None = None,
    ) -> SpawnActionOutput:
        _ = (sink, prepared)
        captured["spawn_id"] = payload.spawn_id
        captured["prompt"] = payload.prompt
        return SpawnActionOutput(command="spawn.continue", status="dry-run")

    monkeypatch.setattr(spawn_cli, "spawn_continue_sync", _fake_spawn_continue_sync)
    monkeypatch.setattr(spawn_cli.sys, "stdin", _FakeStdin("", is_tty=True))

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main(["spawn", "--continue", "p1", "--dry-run"])

    assert exc_info.value.code == 0
    assert captured == {"spawn_id": "p1", "prompt": ""}


def test_spawn_fork_threads_source_claude_config_dir_into_session_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MERIDIAN_DEPTH", "1")
    monkeypatch.setattr(spawn_cli.sys, "stdin", _FakeStdin("", is_tty=True))
    captured: dict[str, object] = {}

    resolved_reference = SimpleNamespace(
        harness_session_id="session-seed",
        harness="claude",
        source_model="claude-sonnet-4-5",
        source_agent="coder",
        source_skills=("review",),
        source_work_id="work-1",
        source_chat_id="c7",
        source_execution_cwd="/tmp/source-cwd",
        source_claude_config_dir="/tmp/source-claude-config",
        tracked=True,
        missing_harness_session_id=False,
    )

    def _fake_spawn_create_sync(
        payload: SpawnCreateInput,
        *,
        sink: object | None = None,
        prepared: Any | None = None,
    ) -> SpawnActionOutput:
        _ = (sink, prepared)
        captured["payload"] = payload
        return SpawnActionOutput(command="spawn.create", status="dry-run")

    monkeypatch.setattr(
        spawn_cli, "resolve_session_reference", lambda *_args, **_kwargs: resolved_reference
    )
    monkeypatch.setattr(spawn_cli, "spawn_create_sync", _fake_spawn_create_sync)

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main(["spawn", "--fork", "c7", "-p", "fork prompt", "--dry-run"])

    assert exc_info.value.code == 0
    payload = captured["payload"]
    assert isinstance(payload, SpawnCreateInput)
    assert payload.session.source_execution_cwd == "/tmp/source-cwd"
    assert payload.session.source_claude_config_dir == "/tmp/source-claude-config"


@pytest.mark.parametrize("prompt_value", ["list", "show", "files", "stats"])
def test_spawn_default_create_routes_correctly_when_prompt_matches_subcommand(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    prompt_value: str,
) -> None:
    monkeypatch.setenv("MERIDIAN_DEPTH", "1")

    def _fake_spawn_create_sync(
        payload: SpawnCreateInput,
        *,
        sink: object | None = None,
        prepared: Any | None = None,
    ) -> SpawnActionOutput:
        _ = (sink, prepared)
        assert payload.prompt == prompt_value
        return SpawnActionOutput(command="spawn.create", status="dry-run")

    monkeypatch.setattr(spawn_cli, "spawn_create_sync", _fake_spawn_create_sync)
    monkeypatch.setattr(spawn_cli.sys, "stdin", _FakeStdin("", is_tty=True))

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main(["--format", "text", "spawn", "-p", prompt_value, "--dry-run"])

    assert exc_info.value.code == 0
    output = capsys.readouterr().out
    assert "Spawn dry-run." in output


@pytest.mark.parametrize(
    "argv",
    [
        ["--format", "text", "spawn", "--continue", "p1", "--dry-run"],
        ["--format", "text", "spawn", "--continue=p1", "--dry-run"],
    ],
)
def test_spawn_continue_routes_correctly(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
) -> None:
    monkeypatch.setenv("MERIDIAN_DEPTH", "1")
    captured: dict[str, str] = {}

    def _fake_spawn_continue_sync(
        payload: SpawnContinueInput,
        *,
        sink: object | None = None,
        prepared: Any | None = None,
    ) -> SpawnActionOutput:
        _ = (sink, prepared)
        captured["spawn_id"] = payload.spawn_id
        return SpawnActionOutput(command="spawn.continue", status="dry-run")

    monkeypatch.setattr(spawn_cli, "spawn_continue_sync", _fake_spawn_continue_sync)
    monkeypatch.setattr(spawn_cli.sys, "stdin", _FakeStdin("", is_tty=True))

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main(argv)

    assert exc_info.value.code == 0
    assert captured["spawn_id"] == "p1"
    output = capsys.readouterr().out
    assert "Spawn dry-run." in output


def test_spawn_explicit_text_format_works(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("MERIDIAN_DEPTH", "1")

    def _fake_spawn_create_sync(
        payload: SpawnCreateInput,
        *,
        sink: object | None = None,
        prepared: Any | None = None,
    ) -> SpawnActionOutput:
        _ = (sink, prepared)
        assert payload.prompt == "build"
        return SpawnActionOutput(command="spawn.create", status="dry-run")

    monkeypatch.setattr(spawn_cli, "spawn_create_sync", _fake_spawn_create_sync)
    monkeypatch.setattr(spawn_cli.sys, "stdin", _FakeStdin("", is_tty=True))

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main(["--format", "text", "spawn", "-p", "build", "--dry-run"])

    assert exc_info.value.code == 0
    output = capsys.readouterr().out
    assert "Spawn dry-run." in output


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


def test_spawn_dry_run_uses_threaded_project_root_for_runtime_write(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    prepared_context = object()
    prepared_roots: list[object] = []

    monkeypatch.delenv("MERIDIAN_DEPTH", raising=False)
    monkeypatch.setattr(
        cli_main,
        "maybe_bootstrap_runtime_state",
        lambda *_args, **_kwargs: project_root,
    )
    monkeypatch.setattr(
        spawn_cli,
        "resolve_project_root",
        lambda: pytest.fail("spawn CLI should reuse the threaded project root"),
    )
    monkeypatch.setattr(
        spawn_cli,
        "prepare_for_runtime_write",
        lambda root: prepared_roots.append(root) or prepared_context,
    )
    monkeypatch.setattr(spawn_cli.sys, "stdin", _FakeStdin("", is_tty=True))

    def _fake_spawn_create_sync(
        payload: SpawnCreateInput,
        *,
        sink: object | None = None,
        prepared: Any | None = None,
    ) -> SpawnActionOutput:
        _ = sink
        assert payload.prompt == "build"
        assert prepared is prepared_context
        return SpawnActionOutput(command="spawn.create", status="dry-run")

    monkeypatch.setattr(spawn_cli, "spawn_create_sync", _fake_spawn_create_sync)

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main(["spawn", "-p", "build", "--dry-run"])

    assert exc_info.value.code == 0
    assert prepared_roots == [project_root]


def test_spawn_dry_run_text_includes_model_routing_provenance(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("MERIDIAN_DEPTH", "1")

    def _fake_spawn_create_sync(
        payload: SpawnCreateInput,
        *,
        sink: object | None = None,
        prepared: Any | None = None,
    ) -> SpawnActionOutput:
        _ = (payload, sink)
        return SpawnActionOutput(
            command="spawn.create",
            status="dry-run",
            message="Dry run complete.",
            model="gpt-5.5",
            harness_id="codex",
            model_selection_requested_token="gpt55",
            model_selection_canonical_id="gpt-5.5",
            model_selection_harness_provenance="pattern-fallback",
        )

    monkeypatch.setattr(spawn_cli, "spawn_create_sync", _fake_spawn_create_sync)
    monkeypatch.setattr(spawn_cli.sys, "stdin", _FakeStdin("", is_tty=True))

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main(["--format", "text", "spawn", "-p", "build", "--dry-run"])

    assert exc_info.value.code == 0
    output = capsys.readouterr().out
    assert "Dry run complete." in output
    assert "Model: gpt-5.5 (codex)" in output
    assert "Routing: pattern-fallback" in output


def test_spawn_dry_run_explicit_json_includes_model_selection(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("MERIDIAN_DEPTH", "1")

    def _fake_spawn_create_sync(
        payload: SpawnCreateInput,
        *,
        sink: object | None = None,
        prepared: Any | None = None,
    ) -> SpawnActionOutput:
        _ = (payload, sink)
        return SpawnActionOutput(
            command="spawn.create",
            status="dry-run",
            model="gpt-5.5",
            harness_id="codex",
            model_selection_requested_token="gpt55",
            model_selection_canonical_id="gpt-5.5",
            model_selection_harness_provenance="pattern-fallback",
        )

    monkeypatch.setattr(spawn_cli, "spawn_create_sync", _fake_spawn_create_sync)
    monkeypatch.setattr(spawn_cli.sys, "stdin", _FakeStdin("", is_tty=True))

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main(["--format", "json", "spawn", "-p", "build", "--dry-run"])

    assert exc_info.value.code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["model_selection"] == {
        "requested_token": "gpt55",
        "canonical_model_id": "gpt-5.5",
        "harness_provenance": "pattern-fallback",
    }


def test_spawn_dry_run_explicit_json_includes_terminal_surface_mode(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("MERIDIAN_DEPTH", "1")

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
            terminal_surface_mode="pty_mediated",
        )

    monkeypatch.setattr(spawn_cli, "spawn_create_sync", _fake_spawn_create_sync)
    monkeypatch.setattr(spawn_cli.sys, "stdin", _FakeStdin("", is_tty=True))

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main(["--format", "json", "spawn", "-p", "build", "--dry-run"])

    assert exc_info.value.code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["terminal_surface_mode"] == "pty_mediated"


def test_spawn_action_output_to_wire_includes_dry_run_model_selection() -> None:
    payload = SpawnActionOutput(
        command="spawn.create",
        status="dry-run",
        model_selection_requested_token="gpt55",
        model_selection_canonical_id="gpt-5.5",
        model_selection_harness_provenance="pattern-fallback",
    ).to_wire()

    assert payload["model_selection"] == {
        "requested_token": "gpt55",
        "canonical_model_id": "gpt-5.5",
        "harness_provenance": "pattern-fallback",
    }


def test_spawn_action_output_to_wire_includes_resolved_authority_for_dry_run() -> None:
    payload = SpawnActionOutput(
        command="spawn.create",
        status="dry-run",
        project_root="/repo",
        project_root_source="explicit",
        runtime_root="/runtime",
        runtime_root_source="env",
    ).to_wire()

    assert payload["resolved_authority"] == {
        "project_root": "/repo",
        "project_root_source": "explicit",
        "runtime_root": "/runtime",
        "runtime_root_source": "env",
    }


def test_spawn_action_output_to_wire_includes_terminal_surface_mode_for_dry_run() -> None:
    payload = SpawnActionOutput(
        command="spawn.create",
        status="dry-run",
        terminal_surface_mode="pty_mediated",
    ).to_wire()

    assert payload["terminal_surface_mode"] == "pty_mediated"


def test_spawn_explicit_json_error_is_structured(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("MERIDIAN_DEPTH", "1")
    monkeypatch.setattr(spawn_cli.sys, "stdin", _FakeStdin("", is_tty=True))

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main(["--format", "json", "spawn", "-p", "literal", "--prompt-file", "-"])

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.strip().startswith("{"), captured.err
    payload = json.loads(captured.err)
    assert payload["error"] == "cannot specify both -p and --prompt-file"
    assert payload["exit_code"] == 1


def test_spawn_implicit_text_error_is_plain(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("MERIDIAN_DEPTH", "1")
    monkeypatch.setattr(spawn_cli.sys, "stdin", _FakeStdin("", is_tty=True))

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main(["--format", "text", "spawn", "-p", "literal", "--prompt-file", "-"])

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "error: cannot specify both -p and --prompt-file\n"


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
    # Events suppressed in agent mode - stderr should be empty
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


def test_spawn_list_active_view_includes_finalizing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MERIDIAN_DEPTH", "1")
    captured: dict[str, SpawnListInput] = {}

    def _fake_spawn_list_sync(
        payload: SpawnListInput,
        *,
        sink: object | None = None,
        prepared: Any | None = None,
    ) -> SpawnListOutput:
        _ = (sink, prepared)
        captured["payload"] = payload
        return SpawnListOutput(spawns=())

    monkeypatch.setattr(spawn_cli, "spawn_list_sync", _fake_spawn_list_sync)

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main(["spawn", "list", "--view", "active"])

    assert exc_info.value.code == 0
    statuses = captured["payload"].statuses
    assert statuses is not None
    assert "finalizing" in statuses


def test_spawn_list_status_accepts_finalizing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MERIDIAN_DEPTH", "1")
    captured: dict[str, SpawnListInput] = {}

    def _fake_spawn_list_sync(
        payload: SpawnListInput,
        *,
        sink: object | None = None,
        prepared: Any | None = None,
    ) -> SpawnListOutput:
        _ = (sink, prepared)
        captured["payload"] = payload
        return SpawnListOutput(spawns=())

    monkeypatch.setattr(spawn_cli, "spawn_list_sync", _fake_spawn_list_sync)

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main(["spawn", "list", "--status", "finalizing"])

    assert exc_info.value.code == 0
    assert captured["payload"].status == "finalizing"
    assert captured["payload"].statuses is None


def test_spawn_list_primary_filter_routes_to_spawn_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MERIDIAN_DEPTH", "1")
    captured: dict[str, SpawnListInput] = {}

    def _fake_spawn_list_sync(
        payload: SpawnListInput,
        *,
        sink: object | None = None,
        prepared: Any | None = None,
    ) -> SpawnListOutput:
        _ = (sink, prepared)
        captured["payload"] = payload
        return SpawnListOutput(spawns=())

    monkeypatch.setattr(spawn_cli, "spawn_list_sync", _fake_spawn_list_sync)

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main(["spawn", "list", "--primary"])

    assert exc_info.value.code == 0
    payload = captured["payload"]
    assert payload.primary is True
    statuses = payload.statuses
    assert statuses is not None
    assert "finalizing" in statuses


def test_spawn_cancel_routes_raw_spawn_id_to_spawn_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MERIDIAN_DEPTH", "1")
    captured: dict[str, object] = {}

    def _fake_spawn_cancel_sync(
        payload,
        *,
        sink: object | None = None,
        prepared: Any | None = None,
    ) -> SpawnActionOutput:
        captured["spawn_id"] = payload.spawn_id
        captured["prepared"] = prepared
        _ = sink
        return SpawnActionOutput(command="spawn.cancel", status="cancelled")

    monkeypatch.setattr(spawn_cli, "spawn_cancel_sync", _fake_spawn_cancel_sync)
    monkeypatch.setattr(spawn_cli, "_prepare_spawn_runtime_write", lambda: "prepared")

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main(["spawn", "cancel", "c213"])

    assert exc_info.value.code == 0
    assert captured == {"spawn_id": "c213", "prepared": "prepared"}


def test_spawn_children_routes_to_spawn_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MERIDIAN_DEPTH", "1")
    captured: dict[str, object] = {}

    def _fake_spawn_children_sync(
        payload,
        *,
        sink: object | None = None,
        prepared: Any | None = None,
    ) -> SpawnListOutput:
        captured["payload"] = payload
        captured["sink"] = sink
        captured["prepared"] = prepared
        return SpawnListOutput(spawns=())

    monkeypatch.setattr(spawn_cli, "spawn_children_sync", _fake_spawn_children_sync)
    emitted: list[SpawnListOutput] = []
    spawn_cli._spawn_children(emitted.append, "c213")

    assert captured["payload"].spawn_id == "c213"
    assert captured["prepared"] is not None
    assert len(emitted) == 1
    assert emitted[0].spawns == ()


def test_spawn_children_includes_agent_and_desc_in_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MERIDIAN_DEPTH", "1")
    output = SpawnListOutput(
        spawns=(
            SpawnListEntry(
                spawn_id="p101",
                status="succeeded",
                model="gpt-5.4",
                agent="coder",
                desc="summarize these long launch semantics for output table",
                duration_secs=1.2,
                cost_usd=0.02,
            ),
        ),
        text_view="children",
    )
    monkeypatch.setattr(spawn_cli, "spawn_children_sync", lambda *_args, **_kwargs: output)

    emitted: list[SpawnListOutput] = []
    spawn_cli._spawn_children(emitted.append, "p100")

    assert len(emitted) == 1
    rendered = emitted[0].format_text()
    assert "agent" in rendered
    assert "desc" in rendered
    assert "coder" in rendered
    assert "summarize these long launch semantics" in rendered


def test_spawn_children_json_includes_agent_and_desc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MERIDIAN_DEPTH", "1")
    output = SpawnListOutput(
        spawns=(
            SpawnListEntry(
                spawn_id="p101",
                status="succeeded",
                model="gpt-5.4",
                agent="reviewer",
                desc="quick desc",
                duration_secs=0.7,
                cost_usd=0.01,
            ),
        ),
        text_view="children",
    )
    monkeypatch.setattr(spawn_cli, "spawn_children_sync", lambda *_args, **_kwargs: output)

    emitted: list[SpawnListOutput] = []
    spawn_cli._spawn_children(emitted.append, "p100")

    payload = emitted[0].model_dump()
    assert payload["spawns"][0]["agent"] == "reviewer"
    assert payload["spawns"][0]["desc"] == "quick desc"


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
    output = capsys.readouterr().out
    assert "reviewer" in output
    assert "review child" in output
