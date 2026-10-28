from __future__ import annotations

import ast
from pathlib import Path


def test_harness_modules_do_not_import_idle_policy() -> None:
    harness_root = Path(__file__).parents[3] / "src" / "meridian" / "lib" / "harness"
    violations: list[str] = []

    for path in harness_root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            imports_idle = False
            if isinstance(node, ast.Import):
                imports_idle = any(
                    alias.name == "meridian.lib.idle" or alias.name.startswith("meridian.lib.idle.")
                    for alias in node.names
                )
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                imports_idle = (
                    node.module == "meridian.lib.idle"
                    or node.module.startswith("meridian.lib.idle.")
                    or (node.level == 2 and node.module == "idle")
                    or (node.level == 2 and node.module.startswith("idle."))
                    or (
                        node.module == "meridian.lib"
                        and any(alias.name == "idle" for alias in node.names)
                    )
                )
            else:
                continue
            if imports_idle:
                violations.append(f"{path.relative_to(harness_root)}:{node.lineno}")

    assert violations == []
