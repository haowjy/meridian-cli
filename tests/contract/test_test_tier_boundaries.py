"""Test-suite tier boundary contracts."""

from __future__ import annotations

import ast
from pathlib import Path, PurePath

_REAL_HARNESS_BINARIES = {"codex", "opencode"}
_AUTOMATED_TEST_DIRS = ("unit", "integration", "contract", "platform")
_LAUNCH_FUNCTIONS = {
    "asyncio.create_subprocess_exec",
    "subprocess.Popen",
    "subprocess.run",
}


def _import_aliases(tree: ast.AST) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in {"asyncio", "subprocess"}:
                    aliases[alias.asname or alias.name] = alias.name
        elif isinstance(node, ast.ImportFrom) and node.module in {"asyncio", "subprocess"}:
            for alias in node.names:
                full_name = f"{node.module}.{alias.name}"
                aliases[alias.asname or alias.name] = full_name
    return aliases


def _call_name(node: ast.AST, aliases: dict[str, str]) -> str | None:
    if isinstance(node, ast.Name):
        return aliases.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        parent = _call_name(node.value, aliases)
        return f"{parent}.{node.attr}" if parent is not None else node.attr
    return None


def _literal_command_head(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.List | ast.Tuple) and node.elts:
        head = node.elts[0]
        if isinstance(head, ast.Constant) and isinstance(head.value, str):
            return head.value
    return None


def _command_arg(node: ast.Call) -> ast.AST | None:
    if node.args:
        return node.args[0]
    for keyword in node.keywords:
        if keyword.arg == "args":
            return keyword.value
    return None


def _normalized_command_name(command_head: str) -> str:
    first_token = command_head.strip().split(maxsplit=1)[0]
    return PurePath(first_token).name


def _automated_python_test_files(root: Path) -> list[Path]:
    tests_root = root / "tests"
    paths: list[Path] = []
    for dirname in _AUTOMATED_TEST_DIRS:
        paths.extend((tests_root / dirname).rglob("*.py"))
    return sorted(path for path in paths if "__pycache__" not in path.parts)


def test_automated_tests_do_not_directly_launch_real_codex_or_opencode() -> None:
    """Real Codex/OpenCode processes belong in manual e2e smoke guides.

    Unit/integration/contract/platform tests may fake Codex/OpenCode process
    boundaries, assert projection output, or monkeypatch subprocess launch
    functions. They must not directly invoke the real harness binaries; those
    tests are too stateful and destructive for the permanent automated suite.
    """

    repo_root = Path(__file__).resolve().parents[2]
    offenders: list[str] = []

    for path in _automated_python_test_files(repo_root):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        aliases = _import_aliases(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if _call_name(node.func, aliases) not in _LAUNCH_FUNCTIONS:
                continue
            command = _command_arg(node)
            if command is None:
                continue
            command_head = _literal_command_head(command)
            if command_head is None:
                continue
            if _normalized_command_name(command_head) in _REAL_HARNESS_BINARIES:
                offenders.append(f"{path.relative_to(repo_root)}:{node.lineno}")

    assert offenders == []
