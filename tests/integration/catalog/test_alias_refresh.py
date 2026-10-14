"""Alias inventory respects dry-run refresh policy at the Mars process boundary."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from meridian.lib.catalog import model_aliases
from meridian.lib.catalog.catalog_session import CatalogSession


@pytest.fixture(autouse=True)
def _project_mars_root(tmp_path: Path) -> None:
    (tmp_path / "mars.toml").write_text("", encoding="utf-8")


def test_alias_inventory_separates_cache_only_from_normal_refresh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        alias = "cached" if "--no-refresh-models" in cmd else "normal"
        return subprocess.CompletedProcess(
            cmd,
            0,
            json.dumps(
                {
                    "aliases": [{"name": alias, "model_id": "openai/test", "harness": "opencode"}],
                }
            ),
            "",
        )

    monkeypatch.setattr(model_aliases, "_resolve_mars_binary", lambda: "/fake/mars")
    monkeypatch.setattr(model_aliases.subprocess, "run", run)
    session = CatalogSession(tmp_path)
    assert set(session.alias_map(no_refresh_models=True)) == {"cached"}
    assert set(session.alias_map()) == {"normal"}
    assert set(session.alias_map(no_refresh_models=True)) == {"cached"}
    assert calls == [
        [
            "/fake/mars",
            "models",
            "aliases",
            "--json",
            "--no-refresh-models",
            "--root",
            str(tmp_path),
        ],
        ["/fake/mars", "models", "aliases", "--json", "--root", str(tmp_path)],
    ]
