# qa-validated: test-suite-redesign
from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

import pytest

from meridian.cli import streaming_serve as streaming_serve_module
from meridian.cli.invocation import GlobalOptions, invocation_scope
from meridian.cli.output import OutputConfig
from meridian.lib.core.execution_policy import ResolvedExecutionPolicy
from meridian.lib.core.types import HarnessId
from meridian.lib.harness.codex_rules import MERIDIAN_RULES_FILE_CONTENT
from meridian.lib.ops.runtime import resolve_runtime_root
from meridian.lib.state.spawn_store import get_spawn
from meridian.lib.streaming.spawn_manager import DrainOutcome
from tests.support.fixtures import write_agent, write_minimal_mars_config
from tests.support.launch import stub_bundle_request_and_resolve


@pytest.fixture(autouse=True)
def _streaming_serve_cli_context(monkeypatch: pytest.MonkeyPatch) -> Generator[None]:
    stub_bundle_request_and_resolve(
        monkeypatch,
        model="gpt-5.4",
        harness=HarnessId.CODEX,
        harness_model="gpt-5.4",
    )
    with invocation_scope(GlobalOptions(output=OutputConfig(format="text"))):
        yield


@pytest.mark.asyncio
async def test_streaming_serve_shutdown_finalizes_once_as_cancelled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_minimal_mars_config(tmp_path)
    runtime_root = resolve_runtime_root(tmp_path)
    helper_calls: list[tuple[str, str]] = []
    codex_home = tmp_path / "codex-home"
    monkeypatch.setenv("CODEX_HOME", codex_home.as_posix())
    write_agent(
        tmp_path,
        name="lead",
        model="gpt-5.4",
        harness="codex",
        sandbox="workspace-write",
        subagents=("reviewer",),
    )
    stub_bundle_request_and_resolve(
        monkeypatch,
        model="gpt-5.4",
        harness=HarnessId.CODEX,
        harness_model="gpt-5.4",
        execution_policy=ResolvedExecutionPolicy(sandbox="workspace-write"),
    )

    async def _run_streaming_spawn(**kwargs: object) -> DrainOutcome:
        helper_calls.append((str(kwargs["spawn_id"]), str(kwargs["runtime_root"])))
        assert (codex_home / "rules" / "meridian.rules").read_text(
            encoding="utf-8"
        ) == MERIDIAN_RULES_FILE_CONTENT
        return DrainOutcome(status="cancelled", exit_code=1)

    monkeypatch.setattr(streaming_serve_module, "run_streaming_spawn", _run_streaming_spawn)

    await streaming_serve_module.streaming_serve("codex", "hello", agent="lead")

    assert helper_calls == [("p1", str(runtime_root))]
    row = get_spawn(runtime_root, "p1")
    assert row is not None
    assert row.status == "cancelled"
    assert row.terminal.exit_code == 1


@pytest.mark.asyncio
async def test_streaming_serve_start_failure_finalizes_failed_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root = resolve_runtime_root(tmp_path)

    async def _run_streaming_spawn(**kwargs: object) -> DrainOutcome:
        _ = kwargs
        raise RuntimeError("boom")

    monkeypatch.setattr(streaming_serve_module, "run_streaming_spawn", _run_streaming_spawn)

    with pytest.raises(RuntimeError, match="boom"):
        await streaming_serve_module.streaming_serve("codex", "hello")

    row = get_spawn(runtime_root, "p1")
    assert row is not None
    assert row.status == "failed"
    assert row.terminal.error == "boom"


@pytest.mark.asyncio
@pytest.mark.parametrize("run_fails", [False, True])
async def test_enrichment_failure_never_blocks_terminal_write(tmp_path, monkeypatch, run_fails):
    async def run(**kwargs):
        if run_fails:
            raise RuntimeError("original run error")
        return DrainOutcome(status="succeeded", exit_code=0)

    def enrich(**kwargs):
        raise OSError("report disk failed")

    monkeypatch.setattr(streaming_serve_module, "run_streaming_spawn", run)
    monkeypatch.setattr(streaming_serve_module, "enrich_finalize", enrich)
    if run_fails:
        with pytest.raises(RuntimeError, match="original run error"):
            await streaming_serve_module.streaming_serve("codex", "hello")
    else:
        await streaming_serve_module.streaming_serve("codex", "hello")
    row = get_spawn(resolve_runtime_root(tmp_path), "p1")
    assert row.status == ("failed" if run_fails else "succeeded")
    assert row.terminal.input_tokens is None
    assert row.terminal.error == ("original run error" if run_fails else None)
