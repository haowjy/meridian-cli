from __future__ import annotations

import re
from pathlib import Path

_SOURCE_ROOT = Path(__file__).resolve().parents[3] / "src"
_POLICY_OWNER_MODULE = _SOURCE_ROOT / "meridian" / "lib" / "chat" / "policy.py"
_NON_OWNER_CHAT_LAUNCH_MODULES = (
    _SOURCE_ROOT / "meridian" / "cli" / "chat_cmd.py",
    _SOURCE_ROOT / "meridian" / "lib" / "chat" / "backend_acquisition.py",
)


def _source_lines(path: Path) -> list[tuple[int, str]]:
    text = path.read_text(encoding="utf-8", errors="replace")
    return list(enumerate(text.splitlines(), start=1))


def _search(path: Path, pattern: str) -> list[tuple[int, str]]:
    compiled = re.compile(pattern)
    return [(lineno, line) for lineno, line in _source_lines(path) if compiled.search(line)]


def _violations(path: Path, label: str, pattern: str) -> list[str]:
    rows: list[str] = []
    for lineno, line in _search(path, pattern):
        stripped = line.strip()
        if stripped.startswith("from ") or stripped.startswith("import "):
            continue
        relative = path.relative_to(_SOURCE_ROOT)
        rows.append(f"{relative}:{lineno}: {label}: {stripped}")
    return rows


def test_chat_policy_owner_module_still_owns_policy_to_launch_conversion() -> None:
    required_owner_calls = [
        ("ComposedLaunchContent", r"\bComposedLaunchContent\s*\("),
        ("resolve_permission_pipeline", r"\bresolve_permission_pipeline\s*\("),
        ("resolve_launch_spec_stage", r"\bresolve_launch_spec_stage\s*\("),
    ]

    missing = [
        label
        for label, pattern in required_owner_calls
        if not _violations(_POLICY_OWNER_MODULE, label, pattern)
    ]
    assert not missing, (
        "chat launch-policy owner drift: "
        f"{_POLICY_OWNER_MODULE.relative_to(_SOURCE_ROOT)} no longer contains: "
        + ", ".join(missing)
    )


def test_non_owner_chat_launch_modules_do_not_compose_launch_specs_directly() -> None:
    forbidden = [
        ("ComposedLaunchContent", r"\bComposedLaunchContent\s*\("),
        ("resolve_permission_pipeline", r"\bresolve_permission_pipeline\s*\("),
        ("resolve_launch_spec_stage", r"\bresolve_launch_spec_stage\s*\("),
        ("ClaudeLaunchSpec", r"\bClaudeLaunchSpec\s*\("),
        ("CodexLaunchSpec", r"\bCodexLaunchSpec\s*\("),
        ("OpenCodeLaunchSpec", r"\bOpenCodeLaunchSpec\s*\("),
        ("UnsafeNoOpPermissionResolver", r"\bUnsafeNoOpPermissionResolver\s*\("),
    ]

    violations: list[str] = []
    for module_path in _NON_OWNER_CHAT_LAUNCH_MODULES:
        for label, pattern in forbidden:
            violations.extend(_violations(module_path, label, pattern))

    assert not violations, "chat launch-policy drift:\n" + "\n".join(violations)
