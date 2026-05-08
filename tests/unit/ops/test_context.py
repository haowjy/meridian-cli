# pyright: reportPrivateUsage=false, reportUnusedFunction=false

"""Unit tests for ops context query centralization."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from meridian.lib.config.context_config import ContextConfig, ContextSourceType
from meridian.lib.config.project_paths import resolve_project_config_paths
from meridian.lib.context.resolver import ResolvedContextPaths
from meridian.lib.core.resolved_context import ResolvedContext
from meridian.lib.ops.context import (
    ContextEntryOutput,
    ContextInput,
    ContextOutput,
    WorkCurrentInput,
    WorkRootInput,
    _resolve_runtime_context,
    context_sync,
    work_current_sync,
    work_root_sync,
)
from meridian.lib.ops.runtime import RuntimeAuthoritySnapshot

if TYPE_CHECKING:
    from pytest import MonkeyPatch


def _strategy_context_entry() -> ContextEntryOutput:
    return ContextEntryOutput(
        source="git",
        path="voluma-bio/strategy",
        resolved="/repo/strategy",
    )


def _authority(
    *,
    project_root: Path = Path("/repo"),
    runtime_root: Path | None = Path("/runtime/state"),
) -> RuntimeAuthoritySnapshot:
    return RuntimeAuthoritySnapshot(
        execution_cwd=project_root,
        project_root=project_root,
        project_root_source="explicit",
        project_config_paths=resolve_project_config_paths(project_root),
        project_state_dir=project_root / ".meridian",
        user_home=Path("/home/user/.meridian"),
        runtime_root=runtime_root,
        runtime_root_source="env" if runtime_root is not None else "unresolved",
    )


@pytest.fixture(autouse=True)
def _clear_context_env(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.delenv("MERIDIAN_CONTEXT_WORK_DIR", raising=False)
    monkeypatch.delenv("MERIDIAN_CONTEXT_KB_DIR", raising=False)
    monkeypatch.delenv("MERIDIAN_ACTIVE_WORK_DIR", raising=False)
    for key in tuple(os.environ):
        if key.startswith("MERIDIAN_CONTEXT_") and key.endswith("_DIR"):
            monkeypatch.delenv(key, raising=False)


@pytest.fixture(autouse=True)
def _stub_runtime_context_lookup(monkeypatch: MonkeyPatch) -> None:
    def fake_resolve_runtime_authority_for_read() -> RuntimeAuthoritySnapshot:
        return _authority()

    def fake_resolve_runtime_context(
        _project_root: Path, _runtime_root: Path
    ) -> ResolvedContext:
        return ResolvedContext()

    monkeypatch.setattr(
        "meridian.lib.ops.context.resolve_runtime_authority_for_read",
        fake_resolve_runtime_authority_for_read,
    )
    monkeypatch.setattr(
        "meridian.lib.ops.context._resolve_runtime_context",
        fake_resolve_runtime_context,
    )


def test_resolve_runtime_context_passes_explicit_roots(
    monkeypatch: MonkeyPatch,
) -> None:
    """_resolve_runtime_context passes roots explicitly — no env mutation."""

    monkeypatch.delenv("MERIDIAN_PROJECT_DIR", raising=False)
    monkeypatch.delenv("MERIDIAN_RUNTIME_DIR", raising=False)

    seen_kwargs: list[dict[str, Any]] = []
    expected = ResolvedContext(depth=7, work_id="w7", work_dir=Path("/repo/.meridian/work/w7"))

    @classmethod  # type: ignore[misc]
    def capturing_from_environment(cls: type[ResolvedContext], **kwargs: Any) -> ResolvedContext:
        seen_kwargs.append(kwargs)
        return expected

    monkeypatch.setattr(ResolvedContext, "from_environment", capturing_from_environment)

    resolved = _resolve_runtime_context(Path("/repo"), Path("/runtime/state"))

    assert resolved is expected
    assert len(seen_kwargs) == 1
    assert seen_kwargs[0]["explicit_project_root"] == Path("/repo")
    assert seen_kwargs[0]["explicit_runtime_root"] == Path("/runtime/state")
    # Env vars must NOT have been mutated.
    assert os.environ.get("MERIDIAN_PROJECT_DIR") is None
    assert os.environ.get("MERIDIAN_RUNTIME_DIR") is None


def test_context_sync_returns_catalog_fields_from_context_resolution(
    monkeypatch: MonkeyPatch,
) -> None:
    project_root = Path("/repo")
    authority = _authority(project_root=project_root)

    def fake_load_context_config(_repo: Path) -> None:
        return None

    def fake_resolve_context_paths(
        _repo: Path,
        config: ContextConfig,
    ) -> ResolvedContextPaths:
        return ResolvedContextPaths(
            work_root=Path("/abs/work"),
            work_archive=Path("/abs/archive/work"),
            work_source=config.work.source,
            kb_root=Path("/abs/kb"),
            kb_source=config.kb.source,
            extra={},
        )

    monkeypatch.setattr(
        "meridian.lib.ops.context.resolve_runtime_authority_for_read",
        lambda: authority,
    )
    monkeypatch.setattr("meridian.lib.ops.context.load_context_config", fake_load_context_config)
    monkeypatch.setattr(
        "meridian.lib.ops.context.resolve_context_paths",
        fake_resolve_context_paths,
    )

    output = context_sync(ContextInput())

    assert output.work_path == ".meridian/work"
    assert output.work_resolved == "/abs/work"
    assert output.work_source == "local"
    assert output.work_archive == ".meridian/archive/work"
    assert output.work_archive_resolved == "/abs/archive/work"
    assert output.kb_path == ".meridian/kb"
    assert output.kb_resolved == "/abs/kb"
    assert output.kb_source == "local"


def test_context_sync_uses_loaded_config_paths_and_sources(monkeypatch: MonkeyPatch) -> None:
    project_root = Path("/repo")
    authority = _authority(project_root=project_root)
    config = ContextConfig.model_validate(
        {
            "work": {
                "source": ContextSourceType.GIT.value,
                "path": "custom/work",
                "archive": "custom/archive",
            },
            "kb": {
                "source": ContextSourceType.LOCAL.value,
                "path": "custom/kb",
            },
        }
    )

    def fake_load_context_config(_repo: Path) -> ContextConfig:
        return config

    def fake_resolve_context_paths(
        _repo: Path,
        _config: ContextConfig,
    ) -> ResolvedContextPaths:
        return ResolvedContextPaths(
            work_root=Path("/resolved/work"),
            work_archive=Path("/resolved/archive"),
            work_source=ContextSourceType.GIT,
            kb_root=Path("/resolved/kb"),
            kb_source=ContextSourceType.LOCAL,
            extra={},
        )

    monkeypatch.setattr(
        "meridian.lib.ops.context.resolve_runtime_authority_for_read",
        lambda: authority,
    )
    monkeypatch.setattr("meridian.lib.ops.context.load_context_config", fake_load_context_config)
    monkeypatch.setattr(
        "meridian.lib.ops.context.resolve_context_paths",
        fake_resolve_context_paths,
    )

    output = context_sync(ContextInput())

    assert output.work_path == "custom/work"
    assert output.work_resolved == "/resolved/work"
    assert output.work_source == "git"
    assert output.work_archive == "custom/archive"
    assert output.work_archive_resolved == "/resolved/archive"
    assert output.kb_path == "custom/kb"
    assert output.kb_resolved == "/resolved/kb"
    assert output.kb_source == "local"


def test_context_sync_includes_arbitrary_named_contexts(monkeypatch: MonkeyPatch) -> None:
    project_root = Path("/repo")
    authority = _authority(project_root=project_root)
    config = ContextConfig.model_validate(
        {
            "strategy": {
                "source": ContextSourceType.GIT.value,
                "remote": "git@github.com:team/docs.git",
                "path": "voluma-bio/strategy",
            },
        }
    )

    def fake_load_context_config(_repo: Path) -> ContextConfig:
        return config

    def fake_resolve_context_paths(
        _repo: Path,
        _config: ContextConfig,
    ) -> ResolvedContextPaths:
        return ResolvedContextPaths(
            work_root=Path("/resolved/work"),
            work_archive=Path("/resolved/archive"),
            work_source=ContextSourceType.LOCAL,
            kb_root=Path("/resolved/kb"),
            kb_source=ContextSourceType.LOCAL,
            extra={
                "strategy": (
                    Path("/home/user/.meridian/git/team-docs/voluma-bio/strategy"),
                    ContextSourceType.GIT,
                )
            },
        )

    monkeypatch.setattr(
        "meridian.lib.ops.context.resolve_runtime_authority_for_read",
        lambda: authority,
    )
    monkeypatch.setattr("meridian.lib.ops.context.load_context_config", fake_load_context_config)
    monkeypatch.setattr(
        "meridian.lib.ops.context.resolve_context_paths",
        fake_resolve_context_paths,
    )

    output = context_sync(ContextInput())

    assert output.extra_contexts["strategy"].source == "git"
    assert output.extra_contexts["strategy"].path == "voluma-bio/strategy"
    assert (
        output.extra_contexts["strategy"].resolved
        == "/home/user/.meridian/git/team-docs/voluma-bio/strategy"
    )
    assert (
        output.resolve_name("strategy")
        == "/home/user/.meridian/git/team-docs/voluma-bio/strategy"
    )


def test_context_output_shows_env_var_names_when_matching(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("MERIDIAN_CONTEXT_WORK_DIR", "/repo/.meridian/work")
    monkeypatch.setenv("MERIDIAN_ACTIVE_WORK_DIR", "/repo/.meridian/work/current")
    monkeypatch.setenv("MERIDIAN_CONTEXT_KB_DIR", "/repo/.meridian/kb")
    monkeypatch.setenv("MERIDIAN_CONTEXT_STRATEGY_DIR", "/repo/strategy")
    output = ContextOutput(
        work_path=".meridian/work",
        work_resolved="/repo/.meridian/work",
        work_source="local",
        work_archive=".meridian/archive/work",
        work_archive_resolved="/repo/.meridian/archive/work",
        kb_path=".meridian/kb",
        kb_resolved="/repo/.meridian/kb",
        kb_source="local",
        extra_contexts={"strategy": _strategy_context_entry()},
    )
    text = output.format_text()
    assert text == (
        "work: $MERIDIAN_ACTIVE_WORK_DIR\n"
        "  archive: /repo/.meridian/archive/work\n"
        "kb: $MERIDIAN_CONTEXT_KB_DIR\n"
        "strategy: $MERIDIAN_CONTEXT_STRATEGY_DIR"
    )


def test_context_output_uses_active_work_env(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("MERIDIAN_ACTIVE_WORK_DIR", "/repo/.meridian/work/some-item")
    monkeypatch.setenv("MERIDIAN_CONTEXT_KB_DIR", "/repo/.meridian/kb")
    output = ContextOutput(
        work_path=".meridian/work",
        work_resolved="/repo/.meridian/work",
        work_source="local",
        work_archive=".meridian/archive/work",
        work_archive_resolved="/repo/.meridian/archive/work",
        kb_path=".meridian/kb",
        kb_resolved="/repo/.meridian/kb",
        kb_source="local",
    )
    text = output.format_text()
    assert "work: $MERIDIAN_ACTIVE_WORK_DIR" in text
    assert "$MERIDIAN_CONTEXT_WORK_DIR" not in text
    assert "kb: $MERIDIAN_CONTEXT_KB_DIR" in text


def test_context_output_shows_paths_when_env_not_set(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.delenv("MERIDIAN_CONTEXT_WORK_DIR", raising=False)
    monkeypatch.delenv("MERIDIAN_CONTEXT_KB_DIR", raising=False)
    monkeypatch.delenv("MERIDIAN_CONTEXT_STRATEGY_DIR", raising=False)
    output = ContextOutput(
        work_path=".meridian/work",
        work_resolved="/repo/.meridian/work",
        work_source="local",
        work_archive=".meridian/archive/work",
        work_archive_resolved="/repo/.meridian/archive/work",
        kb_path=".meridian/kb",
        kb_resolved="/repo/.meridian/kb",
        kb_source="local",
        extra_contexts={"strategy": _strategy_context_entry()},
    )
    text = output.format_text()
    assert text == (
        "work: (no active work item — run 'meridian work start')\n"
        "  archive: /repo/.meridian/archive/work\n"
        "kb: /repo/.meridian/kb\n"
        "strategy: /repo/strategy"
    )


def test_context_output_text_formats_default_and_verbose(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.delenv("MERIDIAN_CONTEXT_WORK_DIR", raising=False)
    monkeypatch.delenv("MERIDIAN_CONTEXT_KB_DIR", raising=False)
    monkeypatch.delenv("MERIDIAN_CONTEXT_STRATEGY_DIR", raising=False)
    output = ContextOutput(
        work_path=".meridian/work",
        work_resolved="/repo/.meridian/work",
        work_source="local",
        work_archive=".meridian/archive/work",
        work_archive_resolved="/repo/.meridian/archive/work",
        kb_path=".meridian/kb",
        kb_resolved="/repo/.meridian/kb",
        kb_source="local",
        extra_contexts={"strategy": _strategy_context_entry()},
    )

    assert (
        output.format_text()
        == "work: (no active work item — run 'meridian work start')\n"
        "  archive: /repo/.meridian/archive/work\n"
        "kb: /repo/.meridian/kb\n"
        "strategy: /repo/strategy"
    )

    verbose_output = output.model_copy(update={"render_verbose": True})
    assert (
        verbose_output.format_text()
        == "work:\n"
        "  source: local\n"
        "  path: .meridian/work\n"
        "  resolved: /repo/.meridian/work\n"
        "  active: (none)\n"
        "  archive: .meridian/archive/work\n"
        "  archive_resolved: /repo/.meridian/archive/work\n"
        "kb:\n"
        "  source: local\n"
        "  path: .meridian/kb\n"
        "  resolved: /repo/.meridian/kb\n"
        "strategy:\n"
        "  source: git\n"
        "  path: voluma-bio/strategy\n"
        "  resolved: /repo/strategy"
    )


def test_context_output_resolve_name_supports_catalog_paths() -> None:
    output = ContextOutput(
        work_path=".meridian/work",
        work_resolved="/repo/.meridian/work",
        work_source="local",
        work_archive=".meridian/archive/work",
        work_archive_resolved="/repo/.meridian/archive/work",
        kb_path=".meridian/kb",
        kb_resolved="/repo/.meridian/kb",
        kb_source="local",
        extra_contexts={"strategy": _strategy_context_entry()},
    )

    assert output.resolve_name("work") == ""
    assert output.resolve_name("kb") == "/repo/.meridian/kb"
    assert output.resolve_name("work.archive") == "/repo/.meridian/archive/work"
    assert output.resolve_name("strategy") == "/repo/strategy"

    try:
        output.resolve_name("unknown")
    except KeyError as exc:
        assert (
            str(exc.args[0])
            == "Unknown context 'unknown'. Expected one of: work, kb, work.archive, strategy."
        )
    else:
        raise AssertionError("Expected KeyError for unknown context lookup")


def test_context_output_resolve_name_returns_active_work_dir() -> None:
    output = ContextOutput(
        work_path=".meridian/work",
        work_resolved="/repo/.meridian/work",
        work_source="local",
        active_work_dir="/repo/.meridian/work/active-item",
        work_archive=".meridian/archive/work",
        work_archive_resolved="/repo/.meridian/archive/work",
        kb_path=".meridian/kb",
        kb_resolved="/repo/.meridian/kb",
        kb_source="local",
    )

    assert output.resolve_name("work") == "/repo/.meridian/work/active-item"


def test_work_current_sync_uses_resolved_context(monkeypatch: MonkeyPatch) -> None:
    project_root = Path("/repo")
    runtime_root = Path("/runtime/state")
    authority = _authority(project_root=project_root, runtime_root=runtime_root)

    def fake_resolve_runtime_context(_repo: Path, _state: Path) -> ResolvedContext:
        return ResolvedContext(work_dir=Path("/repo/.meridian/work/current"))

    monkeypatch.setattr(
        "meridian.lib.ops.context.resolve_runtime_authority_for_read",
        lambda: authority,
    )
    monkeypatch.setattr(
        "meridian.lib.ops.context._resolve_runtime_context",
        fake_resolve_runtime_context,
    )

    output = work_current_sync(WorkCurrentInput())

    assert output.work_dir == "/repo/.meridian/work/current"


def test_work_root_sync_prefers_env(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("MERIDIAN_CONTEXT_WORK_DIR", "/env/work/root")

    output = work_root_sync(WorkRootInput())

    assert output.work_root == "/env/work/root"


def test_work_root_sync_falls_back_to_context_config(monkeypatch: MonkeyPatch) -> None:
    project_root = Path("/repo")
    authority = _authority(project_root=project_root)

    def fake_load_context_config(_repo: Path) -> None:
        return None

    def fake_resolve_context_paths(
        _repo: Path,
        config: ContextConfig,
    ) -> ResolvedContextPaths:
        return ResolvedContextPaths(
            work_root=Path("/resolved/work"),
            work_archive=Path("/resolved/archive/work"),
            work_source=config.work.source,
            kb_root=Path("/resolved/kb"),
            kb_source=config.kb.source,
            extra={},
        )

    monkeypatch.setattr(
        "meridian.lib.ops.context.resolve_runtime_authority_for_read",
        lambda: authority,
    )
    monkeypatch.setattr("meridian.lib.ops.context.load_context_config", fake_load_context_config)
    monkeypatch.setattr(
        "meridian.lib.ops.context.resolve_context_paths",
        fake_resolve_context_paths,
    )

    output = work_root_sync(WorkRootInput())

    assert output.work_root == "/resolved/work"


def test_context_sync_falls_back_to_config_when_session_env_incomplete(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("MERIDIAN_CONTEXT_WORK_DIR", "/env/work/current")
    project_root = Path("/repo")
    authority = _authority(project_root=project_root)

    def fake_load_context_config(_repo: Path) -> None:
        return None

    def fake_resolve_context_paths(
        _repo: Path,
        config: ContextConfig,
    ) -> ResolvedContextPaths:
        return ResolvedContextPaths(
            work_root=Path("/fallback/work"),
            work_archive=Path("/fallback/archive/work"),
            work_source=config.work.source,
            kb_root=Path("/fallback/kb"),
            kb_source=config.kb.source,
            extra={},
        )

    monkeypatch.setattr(
        "meridian.lib.ops.context.resolve_runtime_authority_for_read",
        lambda: authority,
    )
    monkeypatch.setattr("meridian.lib.ops.context.load_context_config", fake_load_context_config)
    monkeypatch.setattr(
        "meridian.lib.ops.context.resolve_context_paths",
        fake_resolve_context_paths,
    )

    output = context_sync(ContextInput())

    assert output.work_resolved == "/fallback/work"
    assert output.kb_resolved == "/fallback/kb"


def test_context_sync_exposes_active_work_from_resolved_runtime_context(
    monkeypatch: MonkeyPatch,
) -> None:
    project_root = Path("/repo")
    runtime_root = Path("/runtime/state")
    authority = _authority(project_root=project_root, runtime_root=runtime_root)

    def fake_load_context_config(_repo: Path) -> None:
        return None

    def fake_resolve_context_paths(
        _repo: Path,
        config: ContextConfig,
    ) -> ResolvedContextPaths:
        return ResolvedContextPaths(
            work_root=Path("/resolved/work"),
            work_archive=Path("/resolved/archive/work"),
            work_source=config.work.source,
            kb_root=Path("/resolved/kb"),
            kb_source=config.kb.source,
            extra={},
        )

    def fake_resolve_runtime_context(_repo: Path, _state: Path) -> ResolvedContext:
        return ResolvedContext(work_dir=Path("/resolved/work/active-work"))

    monkeypatch.setattr(
        "meridian.lib.ops.context.resolve_runtime_authority_for_read",
        lambda: authority,
    )
    monkeypatch.setattr("meridian.lib.ops.context.load_context_config", fake_load_context_config)
    monkeypatch.setattr(
        "meridian.lib.ops.context.resolve_context_paths",
        fake_resolve_context_paths,
    )
    monkeypatch.setattr(
        "meridian.lib.ops.context._resolve_runtime_context",
        fake_resolve_runtime_context,
    )

    output = context_sync(ContextInput())

    assert output.active_work_dir == "/resolved/work/active-work"
    assert output.resolve_name("work") == "/resolved/work/active-work"
