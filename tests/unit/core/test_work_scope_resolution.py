"""Table-driven work-scope resolution at the ResolvedContext / WorkScope seam."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from meridian.lib.core.resolved_context import ResolvedContext
from meridian.lib.ops.context import WorkPathInput, resolve_active_work_scope_dir, work_path_sync
from meridian.lib.ops.runtime import resolve_roots
from meridian.lib.ops.work_attachment import set_session_work_attachment
from meridian.lib.state import session_store, work_store
from meridian.lib.state.paths import resolve_ambient_work_dir
from meridian.lib.state.user_paths import get_project_home
from meridian.lib.state.work_scope import WorkScope, resolve_work_scope_from_parts

if TYPE_CHECKING:
    from pytest import MonkeyPatch


@dataclass(frozen=True)
class _ScopeExpectation:
    kind: str
    identifier: str | None = None
    root_name: str | None = None
    root_suffix: str | None = None
    work_id: str | None = None
    durable: bool | None = None
    ephemeral: bool | None = None


@dataclass(frozen=True)
class _ResolutionCase:
    name: str
    project_id: str
    env: dict[str, str | None]
    explicit: dict[str, str | None]
    session_work_id: str | None = None
    expect: _ScopeExpectation | None = None
    expect_work_dir_is: Path | None = None
    expect_work_dir_is_not: Path | None = None


def _make_backend(session_work_id: str | None) -> MagicMock:
    backend = MagicMock()

    def _resolve_scratch_dir(root: Path, work_id: str) -> Path:
        return root / "work" / work_id

    backend.resolve_work_scratch_dir.side_effect = _resolve_scratch_dir
    backend.get_session_active_work_id.return_value = session_work_id
    return backend


def _apply_env(monkeypatch: MonkeyPatch, env: dict[str, str | None]) -> None:
    for key, value in env.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)


def _resolve_case(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    case: _ResolutionCase,
) -> ResolvedContext:
    project_root = tmp_path / "repo"
    state_dir = project_root / ".meridian"
    state_dir.mkdir(parents=True)
    (state_dir / "id").write_text(case.project_id, encoding="utf-8")

    _apply_env(
        monkeypatch,
        {
            "MERIDIAN_PROJECT_DIR": project_root.as_posix(),
            **case.env,
        },
    )

    backend = _make_backend(case.session_work_id)
    explicit_project_root = (
        Path(case.explicit["project_root"])
        if case.explicit.get("project_root")
        else None
    )
    explicit_runtime_root = (
        Path(case.explicit["runtime_root"])
        if case.explicit.get("runtime_root")
        else None
    )
    explicit_chat_id = case.explicit.get("chat_id")
    explicit_work_id = case.explicit.get("work_id")

    return ResolvedContext.from_environment(
        explicit_project_root=explicit_project_root,
        explicit_runtime_root=explicit_runtime_root,
        explicit_chat_id=explicit_chat_id,
        explicit_work_id=explicit_work_id,
        backend=backend,
    )


def _assert_expectation(ctx: ResolvedContext, expect: _ScopeExpectation) -> None:
    assert ctx.work_scope is not None
    scope = ctx.work_scope
    assert scope.kind == expect.kind
    if expect.identifier is not None:
        assert scope.identifier == expect.identifier
    if expect.work_id is not None:
        assert ctx.work_id == expect.work_id
    if expect.durable is not None:
        assert scope.is_durable == expect.durable
    if expect.ephemeral is not None:
        assert scope.is_ephemeral == expect.ephemeral
    if expect.root_name is not None:
        assert scope.root.name == expect.root_name
    if expect.root_suffix is not None:
        assert scope.root.as_posix().endswith(expect.root_suffix)
    assert ctx.work_dir == scope.root


_RESOLUTION_CASES = (
    _ResolutionCase(
        name="ambient_spawn_no_named_work",
        project_id="proj-ambient",
        env={
            "MERIDIAN_SPAWN_ID": "p42",
            "MERIDIAN_ACTIVE_WORK_ID": None,
            "MERIDIAN_ACTIVE_WORK_DIR": None,
            "MERIDIAN_CHAT_ID": None,
        },
        explicit={},
        expect=_ScopeExpectation(
            kind="ambient_spawn",
            identifier="p42",
            ephemeral=True,
            root_suffix="spawns/p42/work",
        ),
    ),
    _ResolutionCase(
        name="named_work_via_env",
        project_id="proj-named",
        env={
            "MERIDIAN_SPAWN_ID": "p99",
            "MERIDIAN_ACTIVE_WORK_ID": "my-feature",
            "MERIDIAN_ACTIVE_WORK_DIR": None,
            "MERIDIAN_CHAT_ID": None,
        },
        explicit={},
        expect=_ScopeExpectation(
            kind="work_item",
            identifier="my-feature",
            work_id="my-feature",
            durable=True,
            root_name="my-feature",
        ),
    ),
    _ResolutionCase(
        name="bound_dir_without_work_id",
        project_id="proj-env-dir",
        env={
            "MERIDIAN_SPAWN_ID": "p7",
            "MERIDIAN_ACTIVE_WORK_ID": None,
            "MERIDIAN_ACTIVE_WORK_DIR": "__CUSTOM_DIR__",
            "MERIDIAN_CHAT_ID": None,
        },
        explicit={},
        expect=_ScopeExpectation(
            kind="ambient_spawn",
            identifier="p7",
            ephemeral=True,
        ),
    ),
    _ResolutionCase(
        name="session_work_id_canonical_dir",
        project_id="proj-session",
        env={
            "MERIDIAN_SPAWN_ID": "p3",
            "MERIDIAN_ACTIVE_WORK_ID": None,
            "MERIDIAN_ACTIVE_WORK_DIR": None,
            "MERIDIAN_CHAT_ID": "chat-1",
        },
        explicit={},
        session_work_id="session-work",
        expect=_ScopeExpectation(
            kind="work_item",
            identifier="session-work",
            work_id="session-work",
            durable=True,
            root_name="session-work",
        ),
    ),
    _ResolutionCase(
        name="explicit_chat_id_ignores_env_work_scope",
        project_id="proj-explicit-chat",
        env={
            "MERIDIAN_CHAT_ID": "caller-chat",
            "MERIDIAN_ACTIVE_WORK_ID": "caller-work",
            "MERIDIAN_ACTIVE_WORK_DIR": "__CALLER_SCOPE__",
            "MERIDIAN_SPAWN_ID": None,
        },
        explicit={
            "chat_id": "target-chat",
            "project_root": "__PROJECT_ROOT__",
            "runtime_root": "__RUNTIME_ROOT__",
        },
        session_work_id="target-work",
        expect=_ScopeExpectation(
            kind="work_item",
            identifier="target-work",
            work_id="target-work",
            durable=True,
            root_name="target-work",
        ),
    ),
)


@pytest.mark.parametrize("case", _RESOLUTION_CASES, ids=[c.name for c in _RESOLUTION_CASES])
def test_resolved_context_work_scope_rules(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    case: _ResolutionCase,
) -> None:
    project_root = tmp_path / "repo"
    custom_dir = tmp_path / "custom-scope"
    custom_dir.mkdir()
    caller_scope = tmp_path / "caller-scope"
    caller_scope.mkdir()
    runtime_root = get_project_home(case.project_id)

    env = dict(case.env)
    if env.get("MERIDIAN_ACTIVE_WORK_DIR") == "__CUSTOM_DIR__":
        env["MERIDIAN_ACTIVE_WORK_DIR"] = custom_dir.as_posix()
    if env.get("MERIDIAN_ACTIVE_WORK_DIR") == "__CALLER_SCOPE__":
        env["MERIDIAN_ACTIVE_WORK_DIR"] = caller_scope.as_posix()

    explicit = dict(case.explicit)
    if explicit.get("project_root") == "__PROJECT_ROOT__":
        explicit["project_root"] = project_root.as_posix()
    if explicit.get("runtime_root") == "__RUNTIME_ROOT__":
        explicit["runtime_root"] = runtime_root.as_posix()

    resolved_case = _ResolutionCase(
        name=case.name,
        project_id=case.project_id,
        env=env,
        explicit=explicit,
        session_work_id=case.session_work_id,
        expect=case.expect,
        expect_work_dir_is=case.expect_work_dir_is,
        expect_work_dir_is_not=case.expect_work_dir_is_not,
    )
    ctx = _resolve_case(tmp_path, monkeypatch, resolved_case)

    if case.expect is not None:
        _assert_expectation(ctx, case.expect)

    if case.name == "bound_dir_without_work_id":
        assert ctx.work_id is None
        assert ctx.work_dir == custom_dir

    if case.name == "explicit_chat_id_ignores_env_work_scope":
        assert ctx.chat_id == "target-chat"
        assert ctx.work_dir is not None
        assert ctx.work_dir != caller_scope

    if case.name == "named_work_via_env":
        assert ctx.work_dir is not None
        assert "spawns/p99/work" not in ctx.work_dir.as_posix()


def test_session_work_id_ignores_stale_launch_bound_ambient_dir(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    """Regression: session switch must not keep launch-bound ambient MERIDIAN_ACTIVE_WORK_DIR."""

    project_root = tmp_path / "repo"
    state_dir = project_root / ".meridian"
    state_dir.mkdir(parents=True)
    (state_dir / "id").write_text("proj-stale-bound", encoding="utf-8")
    (project_root / "mars.toml").write_text("", encoding="utf-8")
    roots = resolve_roots(project_root.as_posix())
    runtime_root = roots.runtime_root
    project_state_dir = roots.project_state_dir
    stale_ambient = resolve_ambient_work_dir(project_root, "p-launch")

    session_store.start_session(
        runtime_root,
        harness="codex",
        harness_session_id="session-stale",
        model="gpt-5.4",
        chat_id="chat-stale",
    )
    work_item = work_store.create_work_item(project_state_dir, "feature-a", "", None)
    set_session_work_attachment(runtime_root, chat_id="chat-stale", work_id=work_item.name)
    named_scope = work_store.work_scratch_dir(project_state_dir, work_item.name)

    monkeypatch.setenv("MERIDIAN_PROJECT_DIR", project_root.as_posix())
    monkeypatch.setenv("MERIDIAN_SPAWN_ID", "p-launch")
    monkeypatch.setenv("MERIDIAN_CHAT_ID", "chat-stale")
    monkeypatch.setenv("MERIDIAN_ACTIVE_WORK_DIR", stale_ambient.as_posix())
    monkeypatch.delenv("MERIDIAN_ACTIVE_WORK_ID", raising=False)

    ctx = ResolvedContext.from_environment()
    assert ctx.work_id == work_item.name
    assert ctx.work_scope is not None
    assert ctx.work_scope.kind == "work_item"
    assert ctx.work_dir == named_scope.resolve()
    assert ctx.work_dir != stale_ambient.resolve()

    output = work_path_sync(WorkPathInput(relpath="artifact.md"))
    expected = named_scope / "artifact.md"
    assert output.path == expected.resolve().as_posix()
    assert expected.parent.is_dir()


def test_resolve_work_scope_from_parts_prefers_bound_dir_with_env_work_id() -> None:
    bound = Path("/tmp/custom-bound")
    scope = resolve_work_scope_from_parts(
        project_root=Path("/repo"),
        runtime_root=Path("/runtime"),
        spawn_id=None,
        work_id="attached",
        bound_work_dir=bound,
    )

    assert scope == WorkScope(kind="work_item", identifier="attached", root=bound)


def test_resolve_active_work_scope_dir_explicit_chat_id_wiring(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    """Thin ops wiring: explicit chat_id reaches session attachment without env mutation."""

    project_root = tmp_path / "repo"
    state_dir = project_root / ".meridian"
    state_dir.mkdir(parents=True)
    (state_dir / "id").write_text("proj-scope-chat", encoding="utf-8")
    (project_root / "mars.toml").write_text("", encoding="utf-8")
    roots = resolve_roots(project_root.as_posix())
    runtime_root = roots.runtime_root
    project_state_dir = roots.project_state_dir
    caller_scope = tmp_path / "caller-scope"
    caller_scope.mkdir()

    session_store.start_session(
        runtime_root,
        harness="codex",
        harness_session_id="session-target",
        model="gpt-5.4",
        chat_id="target-chat",
    )
    work_item = work_store.create_work_item(project_state_dir, "session-work", "", None)
    set_session_work_attachment(runtime_root, chat_id="target-chat", work_id=work_item.name)
    target_scope = work_store.work_scratch_dir(project_state_dir, work_item.name)

    monkeypatch.setenv("MERIDIAN_PROJECT_DIR", project_root.as_posix())
    monkeypatch.setenv("MERIDIAN_CHAT_ID", "caller-chat")
    monkeypatch.setenv("MERIDIAN_ACTIVE_WORK_DIR", caller_scope.as_posix())
    monkeypatch.delenv("MERIDIAN_ACTIVE_WORK_ID", raising=False)

    scope_dir = resolve_active_work_scope_dir(
        project_root,
        runtime_root,
        chat_id="target-chat",
    )

    assert scope_dir == target_scope.resolve()
    assert scope_dir != caller_scope.resolve()
