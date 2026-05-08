"""Cross-adapter SpawnParams accounting guard tests.

These remain intentionally narrow: SpawnParams field accounting is still an
internal drift risk not fully expressible through the public contract surface.
Behavioral contract tests elsewhere cover bundle registration and projection
conformance.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from meridian.lib.harness.adapter import SpawnParams
from meridian.lib.harness.ids import HarnessId
from meridian.lib.harness.launch_spec import _enforce_spawn_params_accounting

_REPO_ROOT = Path(__file__).resolve().parents[3]


def test_enforce_spawn_params_accounting_reports_missing_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_fields = dict(SpawnParams.model_fields)
    patched_fields = dict(original_fields)
    patched_fields["bogus_phase2_field"] = object()
    monkeypatch.setattr(SpawnParams, "model_fields", patched_fields)

    fake_registry = {
        HarnessId.CLAUDE: SimpleNamespace(
            adapter=SimpleNamespace(handled_fields=frozenset(original_fields))
        )
    }

    with pytest.raises(ImportError, match="bogus_phase2_field"):
        _enforce_spawn_params_accounting(registry=fake_registry)


def test_enforce_spawn_params_accounting_reports_missing_real_field_name() -> None:
    import meridian.lib.harness as harness
    from meridian.lib.harness.bundle import get_bundle_registry

    harness.ensure_bootstrap()
    registry = dict(get_bundle_registry())
    for harness_id, bundle in tuple(registry.items()):
        registry[harness_id] = SimpleNamespace(
            adapter=SimpleNamespace(handled_fields=bundle.adapter.handled_fields - {"mcp_tools"})
        )

    with pytest.raises(ImportError, match=r"Missing .*mcp_tools"):
        _enforce_spawn_params_accounting(registry=registry)


def test_registered_bundle_handled_fields_union_matches_spawn_params() -> None:
    import meridian.lib.harness as harness
    from meridian.lib.harness.bundle import get_bundle_registry

    harness.ensure_bootstrap()
    registry = get_bundle_registry()

    handled_union = frozenset().union(
        *(bundle.adapter.handled_fields for bundle in registry.values())
    )
    assert handled_union == frozenset(SpawnParams.model_fields)


@pytest.mark.parametrize("python_optimize", ("0", "1"))
def test_launch_spec_import_is_clean_in_unmodified_tree(python_optimize: str) -> None:
    env = dict(os.environ)
    env["PYTHONOPTIMIZE"] = python_optimize
    result = subprocess.run(
        [sys.executable, "-c", "import meridian.lib.harness.launch_spec"],
        capture_output=True,
        check=False,
        cwd=_REPO_ROOT,
        env=env,
        text=True,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("python_optimize", ("0", "1"))
def test_harness_package_import_is_clean_in_unmodified_tree(python_optimize: str) -> None:
    env = dict(os.environ)
    env["PYTHONOPTIMIZE"] = python_optimize
    result = subprocess.run(
        [sys.executable, "-c", "import meridian.lib.harness"],
        capture_output=True,
        check=False,
        cwd=_REPO_ROOT,
        env=env,
        text=True,
    )
    assert result.returncode == 0, result.stderr
