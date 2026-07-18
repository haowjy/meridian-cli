from __future__ import annotations

import ast
import re
from pathlib import Path

from meridian.env_registry import is_registered_env_name

_ENV_NAME = re.compile(r"_?MERIDIAN_[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*")
_TS_STRING = re.compile(r'''["'](_?MERIDIAN_[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*)["']''')
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
        elif path.suffix in {".ts", ".tsx", ".js"}:
            literals.update(_TS_STRING.findall(path.read_text(encoding="utf-8")))
    return literals


def test_every_meridian_env_literal_is_registered() -> None:
    unregistered = sorted(
        name for name in _source_env_literals() if not is_registered_env_name(name)
    )
    assert unregistered == []
