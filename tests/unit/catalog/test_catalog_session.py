from __future__ import annotations

import ast
from pathlib import Path

import pytest

from meridian.lib.catalog.catalog_session import CatalogSession
from meridian.lib.catalog.model_aliases import AliasEntry, entry
from meridian.lib.core.types import HarnessId

PROJECT_ROOT = Path("/tmp/catalog-session-project")


def test_catalog_session_reuses_cache_for_repeated_model_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, Path | None]] = []

    def fake_run_mars_models_resolve(
        name: str, project_root: Path | None = None
    ) -> dict[str, object] | None:
        calls.append((name, project_root))
        return {
            "name": name,
            "model_id": "gpt-5.4",
            "harness": "codex",
        }

    monkeypatch.setattr(
        "meridian.lib.catalog.model_aliases.run_mars_models_resolve",
        fake_run_mars_models_resolve,
    )

    session = CatalogSession(PROJECT_ROOT)

    first = session.resolve_model("gpt54")
    second = session.resolve_model("gpt54")

    assert first == second
    assert str(first.model_id) == "gpt-5.4"
    assert first.harness == HarnessId.CODEX
    assert calls == [("gpt54", PROJECT_ROOT)]


def test_catalog_session_alias_map_is_memoized_by_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    aliases = [
        entry(alias=" gpt54 ", model_id="gpt-5.4", harness="codex"),
        entry(alias="", model_id="gpt-5.5", harness="codex"),
    ]
    load_calls = 0

    def fake_load_aliases(self: CatalogSession) -> list[AliasEntry]:
        nonlocal load_calls
        load_calls += 1
        return aliases

    monkeypatch.setattr(CatalogSession, "load_aliases", fake_load_aliases)
    session = CatalogSession(PROJECT_ROOT)

    first = session.alias_map()
    second = session.alias_map()

    assert first is second
    assert load_calls == 1
    assert list(first) == ["gpt54"]
    assert first["gpt54"] == aliases[0]


def test_catalog_session_cache_is_isolated_between_sessions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, Path | None]] = []

    def fake_run_mars_models_resolve(
        name: str, project_root: Path | None = None
    ) -> dict[str, object] | None:
        calls.append((name, project_root))
        return {
            "name": name,
            "model_id": "gpt-5.4",
            "harness": "codex",
        }

    monkeypatch.setattr(
        "meridian.lib.catalog.model_aliases.run_mars_models_resolve",
        fake_run_mars_models_resolve,
    )

    first_session = CatalogSession(PROJECT_ROOT)
    second_session = CatalogSession(PROJECT_ROOT)

    first_session.resolve_model("gpt54")
    second_session.resolve_model("gpt54")

    assert calls == [
        ("gpt54", PROJECT_ROOT),
        ("gpt54", PROJECT_ROOT),
    ]


@pytest.mark.parametrize(
    "forbidden_prefix",
    [
        "meridian.lib.launch",
        "meridian.lib.harness",
        "meridian.lib.env",
        "meridian.lib.permissions",
        "meridian.lib.prompt",
    ],
)
def test_catalog_session_does_not_depend_on_launch_side_modules(
    forbidden_prefix: str,
) -> None:
    module_path = Path("src/meridian/lib/catalog/catalog_session.py")
    tree = ast.parse(module_path.read_text(encoding="utf-8"), filename=str(module_path))

    imported_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported_modules.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module)

    assert all(
        module != forbidden_prefix and not module.startswith(f"{forbidden_prefix}.")
        for module in imported_modules
    )
