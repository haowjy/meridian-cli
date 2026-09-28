"""Mars aliases and raw catalog are distinct machine contracts."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from meridian.lib.catalog import model_aliases
from meridian.lib.catalog.catalog_session import CatalogSession
from meridian.lib.catalog.models import resolve_model
from meridian.lib.ops.catalog import ModelsListInput, models_list_sync

if TYPE_CHECKING:
    import pytest


def test_aliases_and_catalog_use_distinct_argv_keys_and_operation_caches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    def run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        if cmd[2] == "aliases":
            payload = {
                "aliases": [
                    {
                        "name": "demo",
                        "model_id": "openai/demo",
                        "harness": "opencode",
                        "runnable_paths": [
                            {"harness": "opencode", "harness_model_id": "openai/demo"}
                        ],
                    }
                ]
            }
        else:
            payload = {"catalog": [{"id": "openai/demo", "provider": "OpenAI"}]}
        return subprocess.CompletedProcess(cmd, 0, json.dumps(payload), "")

    monkeypatch.setattr(model_aliases, "_resolve_mars_binary", lambda: "/fake/mars")
    monkeypatch.setattr(model_aliases.subprocess, "run", run)
    first = CatalogSession(tmp_path)
    assert first.alias_map()["demo"].model_id == "openai/demo"
    assert first.alias_map()["demo"].mars_provided_harness == "opencode"
    assert first.alias_map()["demo"].runnable_paths == ()
    assert first.load_catalog() == [{"id": "openai/demo", "provider": "OpenAI"}]
    assert first.load_catalog() == [{"id": "openai/demo", "provider": "OpenAI"}]
    assert calls == [
        ["/fake/mars", "models", "aliases", "--json", "--root", str(tmp_path)],
        ["/fake/mars", "models", "catalog", "--json", "--root", str(tmp_path)],
    ]
    CatalogSession(tmp_path).load_catalog()
    assert calls[-1] == calls[1]
    assert len(calls) == 3


def test_malformed_and_failed_aliases_soft_fall_back_to_merged_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    merged = tmp_path / ".mars" / "models-merged.json"
    merged.parent.mkdir()
    merged.write_text(json.dumps({"pinned": {"model": "openai/pinned", "harness": "opencode"}}))
    monkeypatch.setattr(model_aliases, "_resolve_mars_binary", lambda: "/fake/mars")
    responses = iter(
        [
            subprocess.CompletedProcess([], 0, '{"models": []}', ""),
            subprocess.CompletedProcess([], 0, "[]", ""),
            subprocess.CompletedProcess([], 0, "{broken", ""),
            subprocess.CompletedProcess([], 2, "", "failed"),
        ]
    )
    monkeypatch.setattr(model_aliases.subprocess, "run", lambda *_args, **_kwargs: next(responses))
    for _ in range(4):
        aliases = model_aliases.load_mars_aliases(tmp_path)
        assert [(entry.alias, str(entry.model_id)) for entry in aliases] == [
            ("pinned", "openai/pinned")
        ]


def test_catalog_rejects_wrong_or_malformed_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(model_aliases, "_resolve_mars_binary", lambda: "/fake/mars")
    responses = iter(["{}", "{bad", "[]", '{"catalog": ["bad row"]}'])
    monkeypatch.setattr(
        model_aliases.subprocess,
        "run",
        lambda cmd, **_kwargs: subprocess.CompletedProcess(cmd, 0, next(responses), ""),
    )
    for _ in range(4):
        assert model_aliases.run_mars_models_catalog(tmp_path) is None


def test_failed_catalog_is_empty_listing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(model_aliases, "_resolve_mars_binary", lambda: "/fake/mars")
    monkeypatch.setattr(
        model_aliases.subprocess,
        "run",
        lambda cmd, **_kwargs: subprocess.CompletedProcess(cmd, 2, "", "failed"),
    )
    assert models_list_sync(ModelsListInput(project_root=str(tmp_path))).models == ()


def test_exact_id_collision_uses_catalog_without_fabricated_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    def run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        if cmd[2] == "resolve":
            payload = {"name": "gpt-5.4", "model_id": "gpt-5.4-mini", "harness": "codex"}
        else:
            payload = {"catalog": [{"id": "gpt-5.4", "description": "Exact model"}]}
        return subprocess.CompletedProcess(cmd, 0, json.dumps(payload), "")

    monkeypatch.setattr(model_aliases, "_resolve_mars_binary", lambda: "/fake/mars")
    monkeypatch.setattr(model_aliases.subprocess, "run", run)
    entry = resolve_model("gpt-5.4", tmp_path)
    assert str(entry.model_id) == "gpt-5.4"
    assert entry.alias == ""
    assert entry.mars_provided_harness is None
    assert entry.runnable_paths == ()
    assert calls == [
        ["/fake/mars", "models", "resolve", "gpt-5.4", "--json", "--root", str(tmp_path)],
        ["/fake/mars", "models", "catalog", "--json", "--root", str(tmp_path)],
    ]


def test_raw_catalog_listing_maps_fields_without_inventing_routing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = {
        "id": "openai/gpt-test",
        "provider": "OpenAI",
        "description": "Demo model",
        "release_date": "2026-09-01",
        "context_window": 250000,
        "max_output": 80000,
        "cost_input": 1.5,
        "cost_output": 7.5,
        "cost_cache_read": 0.15,
        "cost_cache_write": 2.0,
        "cost_reasoning": 3.0,
        # These are not raw-catalog evidence even if a bad fixture supplies them.
        "harness": "opencode",
        "matched_aliases": ["demo"],
        "family": "fake",
        "capabilities": ["tool"],
        "pinned": True,
    }
    monkeypatch.setattr(model_aliases, "_resolve_mars_binary", lambda: "/fake/mars")
    monkeypatch.setattr(
        model_aliases.subprocess,
        "run",
        lambda cmd, **_kwargs: subprocess.CompletedProcess(
            cmd, 0, json.dumps({"catalog": [raw]}), ""
        ),
    )
    model = models_list_sync(ModelsListInput(project_root=str(tmp_path))).models[0]
    assert model.model_id == "openai/gpt-test"
    assert model.harness is None and model.aliases == ()
    assert model.family is None and model.capabilities == () and not model.pinned
    assert model.context_limit == 250000 and model.output_limit == 80000
    assert model.provider == "OpenAI" and model.description == "Demo model"
    assert model.release_date == "2026-09-01"
    assert (
        model.cost_input,
        model.cost_output,
        model.cost_cache_read,
        model.cost_cache_write,
        model.cost_reasoning,
    ) == (1.5, 7.5, 0.15, 2.0, 3.0)
    wire = model.to_wire()
    assert wire["harness"] is None
    assert "aliases" not in wire and "family" not in wire and "pinned" not in wire
