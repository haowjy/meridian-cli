"""Guard the preflight lints so a rule that stops firing fails loudly."""

from __future__ import annotations

from pathlib import Path

from meridian.dev.lints import (
    RAW_WRITE_ALLOWLIST,
    harness_idle_import_violations,
    launch_path_violations,
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
    plant("meridian/core/dump.py", "def save(f):\n    json.dump({}, f)\n")
    plant("meridian/core/bytes.py", "async def save(path):\n    path.write_bytes(b'')\n")
    plant("meridian/pi_runtime/extensions/env.ts", "const x = process.env.MERIDIAN_PLANTED_TS;\n")
    plant("meridian/core/literal.py", "NAME = 'spawns/p1/report.md'\nSIG = 'x.signal'\n")
    plant("meridian/core/literal.ts", "const f = `${dir}/report.md`;\nresolve_spawn_log_dir();\n")
    plant("meridian/core/retired.py", "resolve_spawn_log_dir\n")
    plant(
        "meridian/core/structure.py",
        "from meridian.lib.state.paths import _HISTORY_FILENAME, LaunchPaths\n"
        "a = LaunchPaths(1)\n"
        "b = LaunchPaths\n"
        "c = replace(launch, n=2)\n"
        "d = LaunchPaths.reserved(1)\n"
        "e = LaunchPaths.preview(1)\n",
    )
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

    assert {v.path for v in raw_write_violations(sources)} == {
        "meridian/core/raw.py",
        "meridian/core/dump.py",
        "meridian/core/bytes.py",
    }
    # `__all__` re-exports are checked by the module that defines them.
    assert {v.path for v in unregistered_env_violations(sources)} == {
        "meridian/core/env.py",
        "meridian/pi_runtime/extensions/env.ts",
    }
    launch = launch_path_violations(sources)
    assert {(v.path, v.line) for v in launch if v.path.endswith("literal.py")} == {
        ("meridian/core/literal.py", 1),
        ("meridian/core/literal.py", 2),
    }
    assert sorted(v.line for v in launch if v.path.endswith("literal.ts")) == [0, 1]
    assert [v.path for v in launch if v.path.endswith("retired.py")] == ["meridian/core/retired.py"]
    assert sorted(v.line for v in launch if v.path.endswith("structure.py")) == [1, 2, 3, 4, 5, 6]
    assert [(v.path, v.line) for v in harness_idle_import_violations(sources)] == [
        ("meridian/lib/harness/planted.py", 1),
        ("meridian/lib/harness/planted.py", 2),
    ]
    # None of the allowlisted writes exist here, so every entry is stale.
    assert {v.path for v in stale_raw_write_allowlist(sources)} == {
        path for path, _, _ in RAW_WRITE_ALLOWLIST
    }
