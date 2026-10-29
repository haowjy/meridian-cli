"""Guard the preflight lints so a rule that stops firing fails loudly."""

from __future__ import annotations

from pathlib import Path

from meridian.dev.lints import (
    RAW_WRITE_ALLOWLIST,
    harness_idle_import_violations,
    raw_write_violations,
    read_sources,
    stale_raw_write_allowlist,
    unregistered_env_violations,
)


def test_each_lint_reports_the_violation_planted_for_it(tmp_path: Path) -> None:
    def plant(relative_path: str, source: str) -> None:
        path = tmp_path / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)

    plant("meridian/core/raw.py", "def save(path):\n    path.write_text('{}')\n")
    plant(
        "meridian/core/env.py",
        "NAME = 'MERIDIAN_PLANTED_UNREGISTERED'\n"
        "__all__ = ['MERIDIAN_REEXPORTED_FROM_ELSEWHERE']\n",
    )
    plant(
        "meridian/lib/harness/planted.py",
        "from meridian.lib.idle import policy\nfrom ..idle import other\n",
    )
    sources = read_sources(tmp_path)

    assert [v.path for v in raw_write_violations(sources)] == ["meridian/core/raw.py"]
    # `__all__` re-exports are checked by the module that defines them.
    assert [v.path for v in unregistered_env_violations(sources)] == ["meridian/core/env.py"]
    assert [(v.path, v.line) for v in harness_idle_import_violations(sources)] == [
        ("meridian/lib/harness/planted.py", 1),
        ("meridian/lib/harness/planted.py", 2),
    ]
    # None of the allowlisted writes exist here, so every entry is stale.
    assert {v.path for v in stale_raw_write_allowlist(sources)} == {
        path for path, _, _ in RAW_WRITE_ALLOWLIST
    }
