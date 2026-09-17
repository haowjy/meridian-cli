"""Mars report protocol survives the subprocess and durable snapshot boundaries."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from meridian.lib.harness.registry import get_default_harness_registry
from meridian.lib.launch import bundle_adapter
from meridian.lib.launch.policy_snapshot import build_launch_policy_snapshot
from meridian.lib.launch.request import SpawnRequest


def report() -> dict[str, object]:
    return {
        "version": 2,
        "scope": {
            "mode": "only",
            "enabled_harnesses": ["codex"],
            "excluded_harnesses": [],
            "target_source": {
                "field": "targets",
                "origin": "local",
                "path": "/project/mars.local.toml",
            },
        },
        "model_attempts": [
            {
                "model_token": "backup",
                "canonical_model": "gpt-5",
                "model_source": "profile-model-policy",
                "assessments": [
                    {
                        "harness": "codex",
                        "installed": True,
                        "verdict": "eligible",
                        "candidate_slugs": ["openai/gpt-5"],
                    }
                ],
            }
        ],
        "selected": {"attempt_index": 0, "assessment_index": 0},
        "outcome": "selected",
    }


def bundle() -> dict[str, object]:
    return {
        "version": 4,
        "routing": {
            "model": "gpt-5",
            "model_token": "backup",
            "harness": "codex",
            "harness_model": "gpt-5",
            "route_trace": report(),
        },
        "execution_policy": {},
    }


def invoke(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, payload: dict[str, object], code: int = 0
):
    binary = tmp_path / "mars"
    binary.write_text(
        f"#!{sys.executable}\nimport sys\nprint({json.dumps(payload)!r})\nsys.exit({code})\n"
    )
    binary.chmod(0o755)
    monkeypatch.setattr(bundle_adapter, "_resolve_mars_binary", lambda: str(binary))
    return bundle_adapter.request_and_resolve(
        bundle_adapter.BundleRequest(agent=None, project_root=tmp_path),
        harness_registry=get_default_harness_registry(),
    )


def test_report_v2_survives_bundle_and_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = invoke(tmp_path, monkeypatch, bundle())
    assert result.selection_report == report()
    request = SpawnRequest(
        prompt="test",
        model=result.model,
        harness=str(result.harness),
        selection_report=result.selection_report,
    )
    snapshot = build_launch_policy_snapshot(request=request)
    assert snapshot.model_dump(mode="json")["selection_report"] == report()
    assert "fallback_chain" not in snapshot.model_dump()


def test_bundle_v3_is_not_accepted_as_report_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = bundle()
    payload["version"] = 3
    with pytest.raises(RuntimeError, match="schema version 3 is unsupported"):
        invoke(tmp_path, monkeypatch, payload)


def test_failed_selection_keeps_structured_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trace = report()
    trace.update(selected=None, outcome="exhausted")
    payload = {
        "error": {"code": "model_candidates_exhausted", "message": "No permitted route"},
        "route_trace": trace,
    }
    with pytest.raises(RuntimeError) as caught:
        invoke(tmp_path, monkeypatch, payload, 2)
    assert getattr(caught.value, "selection_report", None) == trace
    assert getattr(caught.value, "code", None) == "model_candidates_exhausted"
    assert "backup" in str(caught.value)
    from meridian.lib.ops.spawn.models import SpawnCreateInput
    from meridian.lib.ops.spawn.pre_init import PreInitFailure, pre_init_failed_output

    wrapped = PreInitFailure(str(caught.value))
    wrapped.__cause__ = caught.value
    output = pre_init_failed_output(payload=SpawnCreateInput(prompt="test"), exc=wrapped)
    wire = output.to_cli_wire()
    assert wire["selection_report"] == trace
    assert wire["error"] == "model_candidates_exhausted"


@pytest.mark.parametrize(
    "mutation",
    [
        "report-version",
        "boolean-index",
        "past-end",
        "blocked",
        "excluded",
        "model-mismatch",
        "empty-executable",
    ],
)
def test_invalid_report_cannot_authorize_a_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    payload = bundle()
    routing = payload["routing"]
    trace = routing["route_trace"]
    if mutation == "report-version":
        trace["version"] = 2.0
    elif mutation == "boolean-index":
        trace["selected"]["attempt_index"] = True
    elif mutation == "past-end":
        trace["selected"]["assessment_index"] = 8
    elif mutation == "blocked":
        trace["model_attempts"][0]["assessments"][0]["verdict"] = "blocked"
    elif mutation == "excluded":
        trace["scope"]["excluded_harnesses"] = ["codex"]
    elif mutation == "model-mismatch":
        routing["model"] = "other"
    else:
        routing["harness_model"] = ""
    with pytest.raises((ValueError, RuntimeError)):
        invoke(tmp_path, monkeypatch, payload)
