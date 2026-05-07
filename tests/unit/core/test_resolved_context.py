"""Unit tests for immutable resolved runtime context construction."""

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from meridian.lib.config.context_config import ContextConfig
from meridian.lib.core.resolved_context import ContextBackend, ResolvedContext

_MERIDIAN_ENV_KEYS = (
    "MERIDIAN_SPAWN_ID",
    "MERIDIAN_PARENT_SPAWN_ID",
    "MERIDIAN_DEPTH",
    "MERIDIAN_PROJECT_DIR",
    "MERIDIAN_RUNTIME_DIR",
    "MERIDIAN_CHAT_ID",
    "MERIDIAN_ACTIVE_WORK_ID",
    "MERIDIAN_ACTIVE_WORK_DIR",
)


class FakeBackend(ContextBackend):
    def __init__(
        self,
        *,
        session_active_work_id: str | None = None,
        persisted_active_work_id: str | None = None,
        work_dir_suffix: str = "resolved",
    ) -> None:
        self.session_active_work_id = session_active_work_id
        self.persisted_active_work_id = persisted_active_work_id
        self.work_dir_suffix = work_dir_suffix
        self.session_lookup_calls: list[tuple[Path, str]] = []
        self.persisted_lookup_calls: list[Path] = []
        self.work_dir_calls: list[tuple[Path, str]] = []

    def get_session_active_work_id(self, runtime_root: Path, chat_id: str) -> str | None:
        self.session_lookup_calls.append((runtime_root, chat_id))
        return self.session_active_work_id

    def get_persisted_active_work_id(self, runtime_root: Path) -> str | None:
        self.persisted_lookup_calls.append(runtime_root)
        return self.persisted_active_work_id

    def resolve_work_scratch_dir(self, runtime_root: Path, work_id: str) -> Path:
        self.work_dir_calls.append((runtime_root, work_id))
        return runtime_root / "work" / self.work_dir_suffix / work_id


def _clear_meridian_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in _MERIDIAN_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def test_from_environment_without_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    """Canonical resolver output should remain empty/default when env is unset."""
    _clear_meridian_env(monkeypatch)
    backend = FakeBackend()

    resolved = ResolvedContext.from_environment(backend=backend)

    assert resolved.spawn_id is None
    assert resolved.parent_spawn_id is None
    assert resolved.depth == 0
    assert resolved.project_root is None
    assert resolved.runtime_root is None
    assert resolved.chat_id == ""
    assert resolved.work_id is None
    assert resolved.work_dir is None
    assert resolved.kb_dir is None
    assert backend.session_lookup_calls == []
    assert backend.work_dir_calls == []


def test_from_environment_prefers_explicit_work_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """Explicit work override must win over MERIDIAN_ACTIVE_WORK_ID in resolver precedence."""
    _clear_meridian_env(monkeypatch)
    project_root = Path("/repo")
    runtime_root = Path("/runtime/state")
    backend = FakeBackend()

    monkeypatch.setenv("MERIDIAN_PROJECT_DIR", project_root.as_posix())
    monkeypatch.setenv("MERIDIAN_RUNTIME_DIR", runtime_root.as_posix())
    monkeypatch.setenv("MERIDIAN_ACTIVE_WORK_ID", "work-from-env")

    resolved = ResolvedContext.from_environment(
        explicit_work_id="  explicit-work  ",
        backend=backend,
    )

    assert resolved.work_id == "explicit-work"
    assert backend.session_lookup_calls == []
    assert backend.work_dir_calls == []


def test_from_environment_uses_meridian_work_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolver must honor MERIDIAN_ACTIVE_WORK_ID when no explicit override is provided."""
    _clear_meridian_env(monkeypatch)
    project_root = Path("/repo")
    backend = FakeBackend()

    monkeypatch.setenv("MERIDIAN_PROJECT_DIR", project_root.as_posix())
    monkeypatch.setenv("MERIDIAN_ACTIVE_WORK_ID", "work-from-env")

    resolved = ResolvedContext.from_environment(backend=backend)

    assert resolved.work_id == "work-from-env"
    assert resolved.work_dir == Path("/repo/.meridian/work/work-from-env")
    assert resolved.kb_dir == Path("/repo/.meridian/kb")
    assert backend.session_lookup_calls == []
    assert backend.work_dir_calls == []


def test_from_environment_falls_back_to_session_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resolver must consult session active-work lookup only after env sources miss."""
    _clear_meridian_env(monkeypatch)
    runtime_root = Path("/runtime/state")
    backend = FakeBackend(session_active_work_id="active-work", work_dir_suffix="fallback")

    monkeypatch.setenv("MERIDIAN_RUNTIME_DIR", runtime_root.as_posix())
    monkeypatch.setenv("MERIDIAN_CHAT_ID", "c42")

    resolved = ResolvedContext.from_environment(backend=backend)

    assert resolved.work_id == "active-work"
    assert resolved.work_dir == Path("/runtime/state/work/fallback/active-work")
    assert backend.session_lookup_calls == [(runtime_root, "c42")]
    assert backend.persisted_lookup_calls == []
    assert backend.work_dir_calls == [(runtime_root, "active-work")]


def test_from_environment_falls_back_to_persisted_active_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_meridian_env(monkeypatch)
    runtime_root = Path("/runtime/state")
    backend = FakeBackend(persisted_active_work_id="persisted-work")
    monkeypatch.setenv("MERIDIAN_RUNTIME_DIR", runtime_root.as_posix())

    resolved = ResolvedContext.from_environment(backend=backend)

    assert resolved.work_id == "persisted-work"
    assert resolved.work_dir == Path("/runtime/state/work/resolved/persisted-work")
    assert backend.persisted_lookup_calls == [runtime_root]
    assert backend.work_dir_calls == [(runtime_root, "persisted-work")]


def test_from_environment_uses_persisted_fallback_when_session_has_no_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_meridian_env(monkeypatch)
    runtime_root = Path("/runtime/state")
    backend = FakeBackend(session_active_work_id=None, persisted_active_work_id="persisted-work")
    monkeypatch.setenv("MERIDIAN_RUNTIME_DIR", runtime_root.as_posix())
    monkeypatch.setenv("MERIDIAN_CHAT_ID", "c77")

    resolved = ResolvedContext.from_environment(backend=backend)

    assert resolved.work_id == "persisted-work"
    assert backend.session_lookup_calls == [(runtime_root, "c77")]
    assert backend.persisted_lookup_calls == [runtime_root]


def test_from_environment_prefers_active_work_dir_env_over_derived_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_meridian_env(monkeypatch)
    runtime_root = Path("/runtime/state")
    backend = FakeBackend(persisted_active_work_id="persisted-work")
    monkeypatch.setenv("MERIDIAN_RUNTIME_DIR", runtime_root.as_posix())
    monkeypatch.setenv("MERIDIAN_ACTIVE_WORK_DIR", "/override/work-dir")

    resolved = ResolvedContext.from_environment(backend=backend)

    assert resolved.work_dir == Path("/override/work-dir")
    assert resolved.work_id == "persisted-work"


def test_child_env_overrides_output_format() -> None:
    """Child-env projection must serialize the canonical ResolvedContext fields."""
    resolved = ResolvedContext(
        depth=2,
        project_root=Path("/repo"),
        runtime_root=Path("/runtime/state"),
        chat_id="c9",
        work_id="work-123",
        work_dir=Path("/repo/.meridian/work/work-123"),
        kb_dir=Path("/repo/.meridian/kb"),
        context_dirs=(
            ("work", Path("/repo/.meridian/work")),
            ("work_archive", Path("/repo/.meridian/archive/work")),
            ("kb", Path("/repo/.meridian/kb")),
        ),
    )

    overrides = resolved.child_env_overrides()

    assert overrides == {
        "MERIDIAN_DEPTH": "3",
        "MERIDIAN_PROJECT_DIR": "/repo",
        "MERIDIAN_RUNTIME_DIR": "/runtime/state",
        "MERIDIAN_CHAT_ID": "c9",
        "MERIDIAN_ACTIVE_WORK_ID": "work-123",
        "MERIDIAN_ACTIVE_WORK_DIR": "/repo/.meridian/work/work-123",
        "MERIDIAN_CONTEXT_WORK_DIR": "/repo/.meridian/work",
        "MERIDIAN_CONTEXT_WORK_ARCHIVE_DIR": "/repo/.meridian/archive/work",
        "MERIDIAN_CONTEXT_KB_DIR": "/repo/.meridian/kb",
    }
    assert resolved.child_env_overrides(increment_depth=False)["MERIDIAN_DEPTH"] == "2"


def test_from_environment_reads_parent_spawn_id(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_meridian_env(monkeypatch)
    monkeypatch.setenv("MERIDIAN_PARENT_SPAWN_ID", "p-parent")

    resolved = ResolvedContext.from_environment(backend=FakeBackend())

    assert resolved.parent_spawn_id == "p-parent"


def test_child_env_overrides_emits_child_spawn_id() -> None:
    from meridian.lib.core.types import SpawnId

    resolved = ResolvedContext(spawn_id=SpawnId("p-parent"), depth=2)

    overrides = resolved.child_env_overrides(child_spawn_id="p-child")

    assert overrides["MERIDIAN_SPAWN_ID"] == "p-child"


def test_child_env_overrides_emits_parent_spawn_id() -> None:
    from meridian.lib.core.types import SpawnId

    resolved = ResolvedContext(spawn_id=SpawnId("p-parent"), depth=2)

    overrides = resolved.child_env_overrides()

    assert overrides["MERIDIAN_PARENT_SPAWN_ID"] == "p-parent"


def test_child_env_overrides_omits_parent_spawn_id_at_depth_0() -> None:
    resolved = ResolvedContext(depth=0)

    overrides = resolved.child_env_overrides()

    assert "MERIDIAN_PARENT_SPAWN_ID" not in overrides


def test_resolved_context_is_frozen() -> None:
    """ResolvedContext contract requires immutability after resolution."""
    resolved = ResolvedContext(depth=1)

    with pytest.raises(FrozenInstanceError):
        resolved.depth = 2  # type: ignore[misc]


def test_work_dir_prefers_repo_state_root_over_runtime_state_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resolver must derive work_dir from repo-scoped state when project_root exists."""
    _clear_meridian_env(monkeypatch)
    backend = FakeBackend()

    monkeypatch.setenv("MERIDIAN_PROJECT_DIR", "/repo")
    monkeypatch.setenv("MERIDIAN_RUNTIME_DIR", "/runtime/state")
    monkeypatch.setenv("MERIDIAN_ACTIVE_WORK_ID", "selected-work")

    resolved = ResolvedContext.from_environment(backend=backend)

    assert resolved.work_dir == Path("/repo/.meridian/work/selected-work")
    assert backend.work_dir_calls == []


# ---------------------------------------------------------------------------
# Edge case 1: Invalid depth values
# ---------------------------------------------------------------------------


def test_from_environment_negative_depth_clamps_to_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Negative MERIDIAN_DEPTH must clamp to 0, not go negative."""
    _clear_meridian_env(monkeypatch)
    monkeypatch.setenv("MERIDIAN_DEPTH", "-5")

    resolved = ResolvedContext.from_environment(backend=FakeBackend())

    assert resolved.depth == 0


def test_from_environment_non_integer_depth_defaults_to_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-parseable MERIDIAN_DEPTH string must silently default to 0."""
    _clear_meridian_env(monkeypatch)
    monkeypatch.setenv("MERIDIAN_DEPTH", "not-a-number")

    resolved = ResolvedContext.from_environment(backend=FakeBackend())

    assert resolved.depth == 0


# ---------------------------------------------------------------------------
# Edge case 2: Empty env vars treated same as missing
# ---------------------------------------------------------------------------


def test_from_environment_empty_project_root_treated_as_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty MERIDIAN_PROJECT_DIR must be treated as if the variable were absent."""
    _clear_meridian_env(monkeypatch)
    monkeypatch.setenv("MERIDIAN_PROJECT_DIR", "")

    resolved = ResolvedContext.from_environment(backend=FakeBackend())

    assert resolved.project_root is None
    assert resolved.kb_dir is None


def test_from_environment_empty_work_id_env_treated_as_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty MERIDIAN_ACTIVE_WORK_ID must not set work_id or trigger backend calls."""
    _clear_meridian_env(monkeypatch)
    monkeypatch.setenv("MERIDIAN_ACTIVE_WORK_ID", "")
    backend = FakeBackend()

    resolved = ResolvedContext.from_environment(backend=backend)

    assert resolved.work_id is None
    assert resolved.work_dir is None
    # No session lookup because chat_id is also absent
    assert backend.session_lookup_calls == []
    assert backend.work_dir_calls == []


# ---------------------------------------------------------------------------
# Edge case 3: Explicit override precedence — empty/whitespace explicit_work_id
# ---------------------------------------------------------------------------


def test_from_environment_empty_explicit_work_id_falls_back_to_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty explicit_work_id must not override the env-var work ID."""
    _clear_meridian_env(monkeypatch)
    monkeypatch.setenv("MERIDIAN_ACTIVE_WORK_ID", "env-work")
    backend = FakeBackend()

    resolved = ResolvedContext.from_environment(explicit_work_id="", backend=backend)

    assert resolved.work_id == "env-work"


def test_from_environment_explicit_work_id_beats_session_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """explicit_work_id must be used even when a session lookup would return something."""
    _clear_meridian_env(monkeypatch)
    runtime_root = Path("/runtime/state")
    monkeypatch.setenv("MERIDIAN_RUNTIME_DIR", runtime_root.as_posix())
    monkeypatch.setenv("MERIDIAN_CHAT_ID", "c42")
    backend = FakeBackend(session_active_work_id="session-work")

    resolved = ResolvedContext.from_environment(
        explicit_work_id="override-work", backend=backend
    )

    assert resolved.work_id == "override-work"
    # Session lookup must be skipped entirely
    assert backend.session_lookup_calls == []


# ---------------------------------------------------------------------------
# Edge case 5: Work dir derivation when only runtime_root is available (no project_root)
# ---------------------------------------------------------------------------


def test_work_dir_uses_state_root_directly_when_no_project_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When only MERIDIAN_RUNTIME_DIR is set (no MERIDIAN_PROJECT_DIR),
    work_dir must be resolved against runtime_root, not a derived repo state root."""
    _clear_meridian_env(monkeypatch)
    runtime_root = Path("/runtime/state")
    backend = FakeBackend()

    monkeypatch.setenv("MERIDIAN_RUNTIME_DIR", runtime_root.as_posix())
    monkeypatch.setenv("MERIDIAN_ACTIVE_WORK_ID", "my-work")

    resolved = ResolvedContext.from_environment(backend=backend)

    assert resolved.project_root is None
    assert resolved.runtime_root == runtime_root
    assert resolved.work_dir == Path("/runtime/state/work/resolved/my-work")
    assert backend.work_dir_calls == [(runtime_root, "my-work")]
    # kb_dir requires project_root — must be None
    assert resolved.kb_dir is None


# ---------------------------------------------------------------------------
# Edge case: child_env_overrides with all optional fields absent
# ---------------------------------------------------------------------------


def test_child_env_overrides_minimal_context_only_emits_depth() -> None:
    """When all optional fields are None/empty, child_env_overrides must emit only
    MERIDIAN_DEPTH — no spurious keys for absent paths or IDs."""
    resolved = ResolvedContext()  # all defaults: depth=0, everything else None/""

    overrides = resolved.child_env_overrides()

    assert list(overrides.keys()) == ["MERIDIAN_DEPTH"]
    assert overrides["MERIDIAN_DEPTH"] == "1"


def test_from_environment_uses_context_config_for_repo_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_meridian_env(monkeypatch)
    monkeypatch.setenv("MERIDIAN_PROJECT_DIR", "/repo")
    monkeypatch.setenv("MERIDIAN_ACTIVE_WORK_ID", "my-work")
    config = ContextConfig.model_validate(
        {
            "work": {"path": "contexts/work", "archive": "contexts/archive/work"},
            "kb": {"path": "contexts/kb"},
        }
    )

    resolved = ResolvedContext.from_environment(context_config=config, backend=FakeBackend())

    assert resolved.work_dir == Path("/repo/contexts/work/my-work")
    assert resolved.kb_dir == Path("/repo/contexts/kb")


def test_child_env_overrides_exports_arbitrary_context_dirs() -> None:
    resolved = ResolvedContext(
        depth=0,
        context_dirs=(
            ("docs", Path("/contexts/docs")),
            ("team-notes", Path("/contexts/team-notes")),
        ),
    )

    overrides = resolved.child_env_overrides()

    assert overrides["MERIDIAN_CONTEXT_DOCS_DIR"] == "/contexts/docs"
    assert overrides["MERIDIAN_CONTEXT_TEAM_NOTES_DIR"] == "/contexts/team-notes"
