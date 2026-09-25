from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from meridian.env_registry import (
    ENV_VAR_BY_NAME,
    ENV_VARS,
    EnvSubtype,
    EnvTier,
    is_registered_env_name,
)
from meridian.lib.core.child_env import validate_child_env_keys

_ENV_NAME = re.compile(r"_?MERIDIAN_[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*")
_JS_STRING = re.compile(r'''["'](_?MERIDIAN_[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*)["']''')
_JS_ENV_DOT_ACCESS = re.compile(
    r'''process\s*\.\s*env\s*\.\s*(_?MERIDIAN_[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*)\b'''
)
_JS_EXTENSIONS = {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts"}
_SOURCE_ROOT = Path(__file__).parents[2] / "src"


def _python_env_literals(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    exported_symbol_literals: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if not any(isinstance(target, ast.Name) and target.id == "__all__" for target in targets):
            continue
        exported_symbol_literals.update(
            id(child) for child in ast.walk(node.value) if isinstance(child, ast.Constant)
        )

    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in exported_symbol_literals
        and _ENV_NAME.fullmatch(node.value)
    }


def _source_env_literals() -> set[str]:
    literals: set[str] = set()
    for path in _SOURCE_ROOT.rglob("*"):
        if not path.is_file() or "node_modules" in path.parts or "dist" in path.parts:
            continue
        if path.suffix == ".py":
            literals.update(_python_env_literals(path))
        elif path.suffix in _JS_EXTENSIONS:
            source = path.read_text(encoding="utf-8")
            literals.update(_JS_STRING.findall(source))
            literals.update(_JS_ENV_DOT_ACCESS.findall(source))
    return literals


def test_every_meridian_env_literal_is_registered() -> None:
    unregistered = sorted(
        name for name in _source_env_literals() if not is_registered_env_name(name)
    )
    assert unregistered == []


def test_registry_prefix_matches_stability_tier() -> None:
    mismatches = [
        entry.name
        for entry in ENV_VARS
        if entry.name.startswith("_MERIDIAN_") != (entry.tier is EnvTier.INTERNAL)
    ]
    assert mismatches == []


def test_pi_admission_and_session_boundary_handles_are_not_caller_overrides() -> None:
    trusted_pi_handles = {
        "_MERIDIAN_PI_NOTIFICATION_GATE_VERSION",
        "_MERIDIAN_PI_NOTIFICATION_GATE_ATTEMPT",
        "_MERIDIAN_PI_NOTIFICATION_GATE_NONCE",
        "_MERIDIAN_PI_SESSION_BOUNDARY_PATH",
        "_MERIDIAN_PI_SESSION_BOUNDARY_RUN_ID",
        "_MERIDIAN_PI_SESSION_BOUNDARY_ATTEMPT_ID",
        "_MERIDIAN_PI_SESSION_BOUNDARY_SCOPE_ID",
        "_MERIDIAN_PI_SESSION_BOUNDARY_NONCE",
        "_MERIDIAN_PI_SESSION_BOUNDARY_PID",
    }

    assert trusted_pi_handles <= ENV_VAR_BY_NAME.keys()
    for name in trusted_pi_handles:
        contract = ENV_VAR_BY_NAME[name]
        assert contract.tier is EnvTier.INTERNAL
        assert contract.subtype is EnvSubtype.INJECTED_HANDLE
        assert contract.child is False
        with pytest.raises(RuntimeError, match="Unexpected Meridian key"):
            validate_child_env_keys({name: "caller-controlled"})
