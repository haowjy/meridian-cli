"""Unit coverage for harness extraction behavior across adapters."""

import json
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace

from meridian.lib.core.domain import TokenUsage
from meridian.lib.core.types import HarnessId, SpawnId
from meridian.lib.harness.adapter import ArtifactStore
from meridian.lib.harness.bundle import HarnessBundle, HarnessProjectionPorts
from meridian.lib.harness.claude import ClaudeAdapter, project_slug
from meridian.lib.harness.codex import CodexAdapter
from meridian.lib.harness.connections.base import HarnessEvent
from meridian.lib.harness.connections.codex_ws import CodexConnection
from meridian.lib.harness.extractor import StreamingExtractor
from meridian.lib.harness.extractors.base import HarnessExtractor
from meridian.lib.harness.extractors.claude import ClaudeHarnessExtractor
from meridian.lib.harness.ids import TransportId
from meridian.lib.harness.launch_spec import ClaudeLaunchSpec, CodexLaunchSpec, ResolvedLaunchSpec
from meridian.lib.harness.opencode import OpenCodeAdapter
from meridian.lib.launch.extract import enrich_finalize
from meridian.lib.launch.report import extract_or_fallback_report
from meridian.lib.launch.written_files import extract_written_files
from meridian.lib.safety.permissions import UnsafeNoOpPermissionResolver
from meridian.lib.state.artifact_store import InMemoryStore, make_artifact_key


class _StubCodexAdapter(CodexAdapter):
    def __init__(self, *, report: str | None = None, raises: Exception | None = None) -> None:
        self._report = report
        self._raises = raises

    def extract_report(self, artifacts: ArtifactStore, spawn_id: SpawnId) -> str | None:
        _ = artifacts, spawn_id
        if self._raises is not None:
            raise self._raises
        return self._report


class _StubHarnessExtractor(HarnessExtractor[ResolvedLaunchSpec]):
    def __init__(
        self,
        *,
        artifact_session_id: str | None = None,
        fallback_session_id: str | None = None,
    ) -> None:
        self._artifact_session_id = artifact_session_id
        self._fallback_session_id = fallback_session_id

    def detect_session_id_from_event(self, event: HarnessEvent) -> str | None:
        _ = event
        return None

    def detect_session_id_from_artifacts(
        self,
        *,
        spec: ResolvedLaunchSpec,
        launch_env: Mapping[str, str],
        child_cwd: Path,
        runtime_root: Path,
    ) -> str | None:
        _ = spec, launch_env, child_cwd, runtime_root
        return self._fallback_session_id

    def extract_usage(self, artifacts: ArtifactStore, spawn_id: SpawnId) -> TokenUsage:
        _ = artifacts, spawn_id
        return TokenUsage()

    def extract_session_id(self, artifacts: ArtifactStore, spawn_id: SpawnId) -> str | None:
        _ = artifacts, spawn_id
        return self._artifact_session_id

    def extract_report(self, artifacts: ArtifactStore, spawn_id: SpawnId) -> str | None:
        _ = artifacts, spawn_id
        return None


def _bundle_for_extractor(
    extractor: HarnessExtractor[ResolvedLaunchSpec],
) -> HarnessBundle[ResolvedLaunchSpec]:
    return HarnessBundle(
        harness_id=HarnessId.CODEX,
        adapter=SimpleNamespace(
            handled_fields=frozenset(),
            owns_untracked_session=lambda **kwargs: False,
        ),
        spec_cls=CodexLaunchSpec,
        extractor=extractor,
        connections={TransportId.STREAMING: CodexConnection},
        projections=HarnessProjectionPorts(
            subprocess_cli_args=lambda spec, *, base_command: [*base_command, spec.prompt]
        ),
    )


def test_adapters_extract_usage_from_cross_harness_payloads() -> None:
    artifacts = InMemoryStore()

    run_claude = SpawnId("r-claude")
    artifacts.put(
        make_artifact_key(run_claude, "tokens.json"),
        b'{"input_tokens": 1200, "output_tokens": 320, "total_cost_usd": 0.55}',
    )
    claude_usage = ClaudeAdapter().extract_usage(artifacts, run_claude)
    assert claude_usage.input_tokens == 1200
    assert claude_usage.output_tokens == 320
    assert claude_usage.total_cost_usd == 0.55

    run_codex = SpawnId("r-codex")
    artifacts.put(
        make_artifact_key(run_codex, "usage.json"),
        b'{"usage": {"prompt_tokens": "44", "completion_tokens": "12", "cost_usd": "0.04"}}',
    )
    codex_usage = CodexAdapter().extract_usage(artifacts, run_codex)
    assert codex_usage.input_tokens == 44
    assert codex_usage.output_tokens == 12
    assert codex_usage.total_cost_usd == 0.04

    run_opencode = SpawnId("r-opencode")
    artifacts.put(
        make_artifact_key(run_opencode, "history.jsonl"),
        b'{"event":"response.completed","usage":{"input":9,"output":3},"cost":{"total_cost_usd":"0.015"}}\n',
    )
    opencode_usage = OpenCodeAdapter().extract_usage(artifacts, run_opencode)
    assert opencode_usage.input_tokens == 9
    assert opencode_usage.output_tokens == 3
    assert opencode_usage.total_cost_usd == 0.015

    wrapped_opencode = SpawnId("r-opencode-wrapped")
    artifacts.put(
        make_artifact_key(wrapped_opencode, "history.jsonl"),
        b'{"event_type":"response.completed","payload":{"event":"response.completed","usage":{"input":7,"output":2},"cost":{"total_cost_usd":"0.01"}}}\n',
    )
    wrapped_usage = OpenCodeAdapter().extract_usage(artifacts, wrapped_opencode)
    assert wrapped_usage.input_tokens == 7
    assert wrapped_usage.output_tokens == 2
    assert wrapped_usage.total_cost_usd == 0.01


def test_extract_session_ids_from_resume_text_and_json_aliases() -> None:
    artifacts = InMemoryStore()

    codex_spawn = SpawnId("r-codex-resume-text")
    artifacts.put(
        make_artifact_key(codex_spawn, "history.jsonl"),
        b"To continue this session, run codex resume 019cb8d4-8d62-79d3-a925-d329f8310c5d\n",
    )
    assert (
        CodexAdapter().extract_session_id(artifacts, codex_spawn)
        == "019cb8d4-8d62-79d3-a925-d329f8310c5d"
    )

    opencode_json_spawn = SpawnId("r-opencode-sessionid-json")
    artifacts.put(
        make_artifact_key(opencode_json_spawn, "history.jsonl"),
        b'{"type":"session.updated","sessionID":"oc_session_abc123"}\n',
    )
    assert (
        OpenCodeAdapter().extract_session_id(artifacts, opencode_json_spawn) == "oc_session_abc123"
    )

    opencode_text_spawn = SpawnId("r-opencode-sessionid-text")
    artifacts.put(
        make_artifact_key(opencode_text_spawn, "history.jsonl"),
        b"Continue with: opencode --session oc_session_xyz789\n",
    )
    assert (
        OpenCodeAdapter().extract_session_id(artifacts, opencode_text_spawn) == "oc_session_xyz789"
    )

    wrapped_opencode = SpawnId("r-opencode-wrapped-sessionid")
    artifacts.put(
        make_artifact_key(wrapped_opencode, "history.jsonl"),
        b'{"event_type":"session.updated","payload":{"type":"session.updated","sessionID":"oc_wrapped_session"}}\n',
    )
    assert OpenCodeAdapter().extract_session_id(artifacts, wrapped_opencode) == "oc_wrapped_session"


def test_extract_session_id_prefers_history_and_falls_back_to_legacy_output() -> None:
    artifacts = InMemoryStore()

    history_spawn = SpawnId("r-session-history-preferred")
    artifacts.put(
        make_artifact_key(history_spawn, "history.jsonl"),
        b'{"type":"session.updated","sessionID":"history_session"}\n',
    )
    artifacts.put(
        make_artifact_key(history_spawn, "output.jsonl"),
        b'{"type":"session.updated","sessionID":"legacy_session"}\n',
    )

    assert OpenCodeAdapter().extract_session_id(artifacts, history_spawn) == "history_session"

    legacy_spawn = SpawnId("r-session-legacy-output")
    artifacts.put(
        make_artifact_key(legacy_spawn, "output.jsonl"),
        b"To continue this session, run codex resume 019cb8d4-8d62-79d3-a925-d329f8310c5d\n",
    )

    assert (
        CodexAdapter().extract_session_id(artifacts, legacy_spawn)
        == "019cb8d4-8d62-79d3-a925-d329f8310c5d"
    )


def test_harness_extract_report_uses_last_useful_assistant_output() -> None:
    artifacts = InMemoryStore()
    codex_spawn = SpawnId("r-codex-report-last")
    artifacts.put(
        make_artifact_key(codex_spawn, "history.jsonl"),
        b'{"type":"item.completed","item":{"type":"agent_message","text":"first"}}\n'
        b'{"type":"item.completed","item":{"type":"command_execution","aggregated_output":"ignored"}}\n'
        b'{"type":"item.completed","item":{"type":"agent_message","text":"second"}}\n',
    )
    assert CodexAdapter().extract_report(artifacts, codex_spawn) == "second"

    claude_spawn = SpawnId("r-claude-report-result-preferred")
    artifacts.put(
        make_artifact_key(claude_spawn, "history.jsonl"),
        b'{"type":"assistant","content":[{"type":"text","text":"assistant fallback"}]}\n'
        b'{"type":"result","result":"final result report"}\n',
    )
    assert ClaudeAdapter().extract_report(artifacts, claude_spawn) == "final result report"

    opencode_spawn = SpawnId("r-opencode-report-last")
    artifacts.put(
        make_artifact_key(opencode_spawn, "history.jsonl"),
        b'{"type":"assistant","message":"first message"}\n'
        b'{"type":"tool.call","message":"ignored"}\n'
        b'{"type":"assistant","message":"final message"}\n',
    )
    assert OpenCodeAdapter().extract_report(artifacts, opencode_spawn) == "final message"

    wrapped_codex = SpawnId("r-codex-envelope")
    artifacts.put(
        make_artifact_key(wrapped_codex, "history.jsonl"),
        (
            b'{"event_type":"item.completed","payload":{"type":"item.completed","item":'
            b'{"type":"agent_message","text":"wrapped codex report"}}}\n'
        ),
    )
    assert CodexAdapter().extract_report(artifacts, wrapped_codex) == "wrapped codex report"

    wrapped_claude = SpawnId("r-claude-envelope")
    artifacts.put(
        make_artifact_key(wrapped_claude, "history.jsonl"),
        b'{"event_type":"result","payload":{"type":"result","result":"wrapped claude report"}}\n',
    )
    assert ClaudeAdapter().extract_report(artifacts, wrapped_claude) == "wrapped claude report"

    wrapped_opencode = SpawnId("r-opencode-envelope")
    artifacts.put(
        make_artifact_key(wrapped_opencode, "history.jsonl"),
        (
            b'{"event_type":"assistant","payload":{"type":"assistant",'
            b'"message":"wrapped opencode report"}}\n'
        ),
    )
    assert (
        OpenCodeAdapter().extract_report(artifacts, wrapped_opencode) == "wrapped opencode report"
    )


def test_extract_or_fallback_report_tolerates_adapter_errors_and_bad_jsonl() -> None:
    artifacts = InMemoryStore()
    failing_spawn = SpawnId("r-report-adapter-raises")
    artifacts.put(
        make_artifact_key(failing_spawn, "history.jsonl"),
        b'{"role":"assistant","content":"generic fallback"}\n',
    )
    extracted = extract_or_fallback_report(
        artifacts,
        failing_spawn,
        extractor=_StubCodexAdapter(raises=RuntimeError("boom")),
    )
    assert extracted.content == "generic fallback"
    assert extracted.source == "assistant_message"

    malformed_spawn = SpawnId("r-codex-report-malformed-lines")
    artifacts.put(
        make_artifact_key(malformed_spawn, "history.jsonl"),
        b'{"type":"item.completed","item":{"type":"agent_message","text":"first"}}\n'
        b"{this is not valid json}\n"
        b'{"type":"item.completed","item":{"type":"agent_message","text":"second"}}\n'
        b'{"type":"item.completed","item":{"type":"agent_message","text":"unterminated"}\n',
    )
    assert CodexAdapter().extract_report(artifacts, malformed_spawn) == "second"

    whitespace_spawn = SpawnId("r-report-adapter-whitespace")
    artifacts.put(
        make_artifact_key(whitespace_spawn, "history.jsonl"),
        b'{"role":"assistant","content":"generic fallback"}\n',
    )
    blank_extracted = extract_or_fallback_report(
        artifacts,
        whitespace_spawn,
        extractor=_StubCodexAdapter(report="   \n\t"),
    )
    assert blank_extracted.content == "generic fallback"
    assert blank_extracted.source == "assistant_message"


def test_extract_written_files_prefers_explicit_artifacts_only() -> None:
    artifacts = InMemoryStore()
    spawn_id = SpawnId("r-files-explicit")
    artifacts.put(
        make_artifact_key(spawn_id, "written_files.json"),
        (
            b'{"written_files":["src/kept.py"],"events":[{"path":"docs/also-kept.md"}],'
            b'"ignored":"tests/read_only_fixture.py"}'
        ),
    )
    artifacts.put(
        make_artifact_key(spawn_id, "written_files.txt"),
        b"scripts/finalize.sh\nsrc/kept.py\n",
    )
    artifacts.put(
        make_artifact_key(spawn_id, "history.jsonl"),
        b'{"type":"tool.call","tool":"read","path":"src/should_not_leak.py"}\n',
    )
    artifacts.put(
        make_artifact_key(spawn_id, "report.md"),
        b"Reviewed `docs/mentioned_only.md` while working.\n",
    )

    assert extract_written_files(artifacts, spawn_id) == (
        "src/kept.py",
        "docs/also-kept.md",
        "scripts/finalize.sh",
    )


def test_extract_written_files_ignores_report_and_output_without_explicit_signal() -> None:
    artifacts = InMemoryStore()
    spawn_id = SpawnId("r-files-no-fallback")
    artifacts.put(
        make_artifact_key(spawn_id, "history.jsonl"),
        b'{"type":"tool.call","tool":"read","path":"src/read_only.py"}\n',
    )
    artifacts.put(
        make_artifact_key(spawn_id, "report.md"),
        b"Referenced `docs/mentioned_only.md` and `scripts/check-mermaid.sh`.\n",
    )

    assert extract_written_files(artifacts, spawn_id) == ()


def test_extract_or_fallback_report_handles_streaming_codex_terminal_frames() -> None:
    artifacts = InMemoryStore()
    codex_spawn = SpawnId("r-codex-streaming-report")
    artifacts.put(
        make_artifact_key(codex_spawn, "history.jsonl"),
        (
            b'{"event_type":"item/completed","payload":{"item":{"id":"msg-1",'
            b'"type":"agentMessage","text":"streamed report"}}}\n'
            b'{"event_type":"turn/completed","payload":{"threadId":"thread-1","turn":{"id":"turn-1","status":"completed","error":null,"items":[]}}}\n'
        ),
    )

    assert CodexAdapter().extract_report(artifacts, codex_spawn) == "streamed report"

    extracted = extract_or_fallback_report(artifacts, codex_spawn, extractor=CodexAdapter())
    assert extracted.content == "streamed report"
    assert extracted.source == "assistant_message"


def test_extract_or_fallback_report_ignores_cancelled_control_frame_fallback() -> None:
    artifacts = InMemoryStore()
    spawn_id = SpawnId("r-report-cancel-control-frame")
    artifacts.put(
        make_artifact_key(spawn_id, "history.jsonl"),
        b'{"event_type":"cancelled","payload":{"status":"cancelled","exit_code":143,"error":"cancelled"}}\n',
    )

    extracted = extract_or_fallback_report(artifacts, spawn_id, extractor=None)

    assert extracted.content is None
    assert extracted.source is None


def test_enrich_finalize_treats_history_as_output(tmp_path: Path) -> None:
    artifacts = InMemoryStore()
    spawn_id = SpawnId("r-finalize-history-output")
    artifacts.put(
        make_artifact_key(spawn_id, "history.jsonl"),
        b'{"event_type":"tool.call","payload":{"name":"shell"}}\n',
    )

    finalized = enrich_finalize(
        artifacts=artifacts,
        extractor=_StubHarnessExtractor(),
        spawn_id=spawn_id,
        log_dir=tmp_path,
    )

    assert finalized.output_is_empty is False


def test_enrich_finalize_falls_back_to_legacy_output_for_empty_check(tmp_path: Path) -> None:
    artifacts = InMemoryStore()
    spawn_id = SpawnId("r-finalize-legacy-output")
    artifacts.put(
        make_artifact_key(spawn_id, "output.jsonl"),
        b'{"event_type":"tool.call","payload":{"name":"shell"}}\n',
    )

    finalized = enrich_finalize(
        artifacts=artifacts,
        extractor=_StubHarnessExtractor(),
        spawn_id=spawn_id,
        log_dir=tmp_path,
    )

    assert finalized.output_is_empty is False


def test_enrich_finalize_estimates_cost_from_model_catalog(tmp_path: Path) -> None:
    class _UsageExtractor(_StubHarnessExtractor):
        def extract_usage(self, artifacts: ArtifactStore, spawn_id: SpawnId) -> TokenUsage:
            _ = artifacts, spawn_id
            return TokenUsage(input_tokens=1_000_000, output_tokens=500_000)

    artifacts = InMemoryStore()
    spawn_id = SpawnId("r-finalize-cost-estimate")
    (tmp_path / ".mars").mkdir()
    (tmp_path / ".mars" / "models-cache.json").write_text(
        json.dumps(
            {
                "models": [
                    {
                        "id": "gpt-test-model",
                        "cost_input": 2.0,
                        "cost_output": 4.0,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    finalized = enrich_finalize(
        artifacts=artifacts,
        extractor=_UsageExtractor(),
        spawn_id=spawn_id,
        log_dir=tmp_path,
        model_id="gpt-test-model",
        project_root=tmp_path,
    )

    assert finalized.usage.total_cost_usd == 4.0
    assert finalized.usage.cost_is_estimate is True


def test_streaming_extractor_prefers_live_connection_session_id() -> None:
    class _LiveConnection:
        session_id = "thread-live-123"

    spec = CodexLaunchSpec(
        prompt="hello",
        permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
    )
    extractor = StreamingExtractor(
        connection=_LiveConnection(),  # type: ignore[arg-type]
        bundle=_bundle_for_extractor(_StubHarnessExtractor()),
        spec=spec,
        launch_env={},
        child_cwd=Path.cwd(),
        runtime_root=Path.cwd(),
    )

    assert extractor.extract_session_id(InMemoryStore(), SpawnId("p-live")) == "thread-live-123"


def test_streaming_extractor_falls_back_to_harness_owned_artifact_detection() -> None:
    spec = CodexLaunchSpec(
        prompt="hello",
        permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
    )
    extractor = StreamingExtractor(
        connection=None,
        bundle=_bundle_for_extractor(
            _StubHarnessExtractor(
                artifact_session_id=None,
                fallback_session_id="thread-fallback-456",
            )
        ),
        spec=spec,
        launch_env={"CODEX_HOME": "/tmp/nonexistent"},
        child_cwd=Path.cwd(),
        runtime_root=Path.cwd(),
    )

    assert (
        extractor.extract_session_id(InMemoryStore(), SpawnId("p-fallback"))
        == "thread-fallback-456"
    )


def test_observe_session_id_prefers_current_session_before_primary_detection() -> None:
    class _Adapter(CodexAdapter):
        def detect_primary_session_id(
            self,
            *,
            project_root: Path,
            started_at_epoch: float,
            started_at_local_iso: str | None,
        ) -> str | None:
            _ = project_root, started_at_epoch, started_at_local_iso
            raise AssertionError(
                "primary-session detection should not run when current_session_id exists"
            )

    adapter = _Adapter()
    observed = adapter.observe_session_id(
        artifacts=InMemoryStore(),
        spawn_id=None,
        current_session_id=" seeded-session-id ",
        connection_session_id=None,
        project_root=Path.cwd(),
        started_at_epoch=1.0,
        started_at_local_iso="2026-01-01T00:00:00",
    )

    assert observed == "seeded-session-id"


def test_claude_detect_primary_strict_verify_only(tmp_path: Path, monkeypatch) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    claude_config = tmp_path / "claude-config"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_config))

    project_dir = claude_config / "projects" / project_slug(project_root)
    project_dir.mkdir(parents=True)
    session_file = project_dir / "expected-claude-session.jsonl"
    session_file.write_text('{"sessionId":"expected-claude-session"}\n', encoding="utf-8")

    observed = ClaudeAdapter().detect_primary_session_id(
        project_root=project_root,
        started_at_epoch=0.0,
        started_at_local_iso=None,
        expected_session_id="expected-claude-session",
    )

    assert observed == "expected-claude-session"


def test_claude_detect_primary_no_expected_returns_none(tmp_path: Path, monkeypatch) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    claude_config = tmp_path / "claude-config"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_config))

    project_dir = claude_config / "projects" / project_slug(project_root)
    project_dir.mkdir(parents=True)
    session_file = project_dir / "unrelated-claude-session.jsonl"
    session_file.write_text('{"sessionId":"unrelated-claude-session"}\n', encoding="utf-8")

    observed = ClaudeAdapter().detect_primary_session_id(
        project_root=project_root,
        started_at_epoch=0.0,
        started_at_local_iso=None,
    )

    assert observed is None


def test_claude_detect_primary_embedded_mismatch_returns_none(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    claude_config = tmp_path / "claude-config"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_config))

    project_dir = claude_config / "projects" / project_slug(project_root)
    project_dir.mkdir(parents=True)
    session_file = project_dir / "expected-claude-session.jsonl"
    session_file.write_text('{"sessionId":"different-claude-session"}\n', encoding="utf-8")

    observed = ClaudeAdapter().detect_primary_session_id(
        project_root=project_root,
        started_at_epoch=0.0,
        started_at_local_iso=None,
        expected_session_id="expected-claude-session",
    )

    assert observed is None


def test_claude_extractor_no_scan_when_ids_absent(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    home = tmp_path / "home"
    project_dir = home / ".claude" / "projects" / project_slug(project_root)
    project_dir.mkdir(parents=True)
    session_file = project_dir / "heuristic-claude-session.jsonl"
    session_file.write_text('{"sessionId":"heuristic-claude-session"}\n', encoding="utf-8")
    spec = ClaudeLaunchSpec(
        prompt="hello",
        permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
    )

    observed = ClaudeHarnessExtractor().detect_session_id_from_artifacts(
        spec=spec,
        launch_env={"HOME": str(home)},
        child_cwd=project_root,
        runtime_root=tmp_path,
    )

    assert observed is None


def test_claude_extractor_trusts_seeded_session_id() -> None:
    spec = ClaudeLaunchSpec(
        prompt="hello",
        extra_args=("--session-id", "seeded-claude-session"),
        permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
    )

    assert (
        ClaudeHarnessExtractor().detect_session_id_from_artifacts(
            spec=spec,
            launch_env={},
            child_cwd=Path("/tmp/nonexistent-meridian-claude-project"),
            runtime_root=Path.cwd(),
        )
        == "seeded-claude-session"
    )


def test_claude_extractor_prefers_continue_session_id() -> None:
    spec = ClaudeLaunchSpec(
        prompt="hello",
        continue_session_id="continued-claude-session",
        extra_args=("--session-id", "seeded-claude-session"),
        permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
    )

    assert (
        ClaudeHarnessExtractor().detect_session_id_from_artifacts(
            spec=spec,
            launch_env={},
            child_cwd=Path("/tmp/nonexistent-meridian-claude-project"),
            runtime_root=Path.cwd(),
        )
        == "continued-claude-session"
    )


def test_claude_result_usage_sums_per_model_cache_and_exact_cost() -> None:
    artifacts = InMemoryStore()
    spawn_id = SpawnId("r-claude-specific")
    artifacts.put(
        make_artifact_key(spawn_id, "history.jsonl"),
        (
            b'{"type":"result","total_cost_usd":0.123,"usage":{'
            b'"claude-sonnet-4-5":{"input_tokens":10,"output_tokens":4,'
            b'"cache_read_input_tokens":100,"cache_creation_input_tokens":20},'
            b'"claude-haiku-4-5":{"input_tokens":2,"output_tokens":1,'
            b'"cache_read_input_tokens":30,"cache_creation_input_tokens":5}}}\n'
        ),
    )

    usage = ClaudeAdapter().extract_usage(artifacts, spawn_id)

    assert usage.input_tokens == 12
    assert usage.output_tokens == 5
    assert usage.cache_read_input_tokens == 130
    assert usage.cache_creation_input_tokens == 25
    assert usage.total_cost_usd == 0.123
    assert usage.cost_is_estimate is False


def test_codex_turn_completed_uses_last_cumulative_usage() -> None:
    artifacts = InMemoryStore()
    spawn_id = SpawnId("r-codex-specific")
    artifacts.put(
        make_artifact_key(spawn_id, "history.jsonl"),
        (
            b'{"event_type":"turn/completed","usage":{"input_tokens":10,'
            b'"output_tokens":2,"cached_input_tokens":3,"reasoning_output_tokens":1}}\n'
            b'{"event_type":"turn/completed","usage":{"input_tokens":20,'
            b'"output_tokens":5,"cached_input_tokens":7,"reasoning_output_tokens":4}}\n'
        ),
    )

    usage = CodexAdapter().extract_usage(artifacts, spawn_id)

    assert usage.input_tokens == 20
    assert usage.output_tokens == 5
    assert usage.cache_read_input_tokens == 7
    assert usage.reasoning_tokens == 4


def test_opencode_usage_reads_session_idle_info_and_cost() -> None:
    artifacts = InMemoryStore()
    spawn_id = SpawnId("r-opencode-specific")
    artifacts.put(
        make_artifact_key(spawn_id, "history.jsonl"),
        (
            b'{"event":"message.updated","info":{"input_tokens":5,"output_tokens":1}}\n'
            b'{"event":"session.idle","properties":{"info":{"tokens":{"input":9,'
            b'"output":3,"cache":{"read":11}},"cost":0.015}}}\n'
        ),
    )

    usage = OpenCodeAdapter().extract_usage(artifacts, spawn_id)

    assert usage.input_tokens == 9
    assert usage.output_tokens == 3
    assert usage.cache_read_input_tokens == 11
    assert usage.total_cost_usd == 0.015


def test_opencode_usage_reads_nested_message_updated_tokens_and_cost() -> None:
    artifacts = InMemoryStore()
    spawn_id = SpawnId("r-opencode-nested-message-updated")
    artifacts.put(
        make_artifact_key(spawn_id, "history.jsonl"),
        (
            b'{"event":"message.updated","properties":{"info":{"tokens":{"input":7,'
            b'"output":4,"reasoning":2,"cache":{"read":5,"write":1}},"cost":"0.02"}}}\n'
            b'{"event":"session.idle","properties":{"status":{"type":"idle"}}}\n'
        ),
    )

    usage = OpenCodeAdapter().extract_usage(artifacts, spawn_id)

    assert usage.input_tokens == 7
    assert usage.output_tokens == 4
    assert usage.reasoning_tokens == 2
    assert usage.cache_read_input_tokens == 5
    assert usage.cache_creation_input_tokens == 1
    assert usage.total_cost_usd == 0.02


def test_opencode_usage_mixed_nested_and_legacy_fallback_still_works() -> None:
    artifacts = InMemoryStore()
    spawn_id = SpawnId("r-opencode-mixed-usage")
    artifacts.put(
        make_artifact_key(spawn_id, "history.jsonl"),
        (
            b'{"event":"message.updated","properties":{"info":{"tokens":{"input":8,"output":2}}}}\n'
            b'{"event":"response.completed","usage":{"input":13,"output":5,"cache_read":7,'
            b'"cache_write":4,"reasoning":1},"cost":{"total_cost_usd":"0.03"}}\n'
        ),
    )

    usage = OpenCodeAdapter().extract_usage(artifacts, spawn_id)

    assert usage.input_tokens == 13
    assert usage.output_tokens == 5
    assert usage.cache_read_input_tokens == 7
    assert usage.cache_creation_input_tokens == 4
    assert usage.reasoning_tokens == 1
    assert usage.total_cost_usd == 0.03


def test_opencode_usage_control_events_without_tokens_do_not_erase_previous_usage() -> None:
    artifacts = InMemoryStore()
    spawn_id = SpawnId("r-opencode-usage-preserved")
    artifacts.put(
        make_artifact_key(spawn_id, "history.jsonl"),
        (
            b'{"event":"message.updated","properties":{"info":{"tokens":{"input":6,"output":2},'
            b'"cost":0.01}}}\n'
            b'{"event":"session.idle","properties":{"info":{"id":"msg-final"}}}\n'
        ),
    )

    usage = OpenCodeAdapter().extract_usage(artifacts, spawn_id)

    assert usage.input_tokens == 6
    assert usage.output_tokens == 2
    assert usage.total_cost_usd == 0.01
