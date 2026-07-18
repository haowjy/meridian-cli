"""Harness package bootstrap."""

# pyright: reportPrivateUsage=false
# ruff: noqa: I001

from __future__ import annotations

_bootstrapped = False
_bootstrap_imports: tuple[object, ...] = ()

# Adding one bundle-backed streaming harness requires touching the
# harness-specific seams below. Generic dispatch helpers such as
# `harness/connections/__init__.py` are intentionally omitted.
HARNESS_EXTENSION_TOUCHPOINTS: tuple[str, ...] = (
    "src/meridian/lib/core/types.py (HarnessId/TransportId registration)",
    (
        "src/meridian/lib/harness/<new_harness>.py "
        "(adapter + explicit HarnessContract + bundle registration + transport map side effect)"
    ),
    "src/meridian/lib/harness/__init__.py::_run_bootstrap() (bootstrap import wiring)",
    (
        "src/meridian/lib/harness/projections/project_<new_harness>_subprocess.py "
        "+ project_<new_harness>_streaming.py (spec/workspace projection seams)"
    ),
    (
        "src/meridian/lib/harness/extractors/<new_harness>.py "
        "(report/session/usage extraction seams)"
    ),
    (
        "src/meridian/lib/harness/registry.py::HarnessRegistry.with_defaults() "
        "(default adapter registry)"
    ),
    (
        "src/meridian/lib/harness/launch_spec.py::"
        "_enforce_spawn_params_accounting() (handled_fields drift guard)"
    ),
    (
        "src/meridian/lib/harness/connections/<new_harness>_<transport>.py "
        "(concrete streaming transport implementation)"
    ),
    (
        "src/meridian/lib/harness/projections/permission_flags.py "
        "(approval/sandbox CLI flag projection)"
    ),
)


def _run_bootstrap() -> None:
    """Execute the load-bearing harness bootstrap sequence exactly once."""

    global _bootstrapped
    global _bootstrap_imports

    if _bootstrapped:
        return

    # Import order is load-bearing.
    #
    # 1) Adapter modules register HarnessBundle entries as module-load side effects.
    # 2) Projection modules execute import-time drift guards.
    # 3) Extractor modules bind runtime-checkable Protocol implementations.
    # 4) Cross-adapter SpawnParams accounting runs after all registrations.
    from meridian.lib.harness import claude as _claude
    from meridian.lib.harness import codex as _codex
    from meridian.lib.harness import cursor as _cursor
    from meridian.lib.harness import opencode as _opencode
    from meridian.lib.harness import pi as _pi

    from meridian.lib.harness.projections import project_claude as _project_claude
    from meridian.lib.harness.projections import project_cursor as _project_cursor
    from meridian.lib.harness.projections import (
        project_codex_streaming as _project_codex_streaming,
    )
    from meridian.lib.harness.projections import (
        project_codex_subprocess as _project_codex_subprocess,
    )
    from meridian.lib.harness.projections import (
        project_opencode_streaming as _project_opencode_streaming,
    )
    from meridian.lib.harness.projections import (
        project_opencode_subprocess as _project_opencode_subprocess,
    )
    from meridian.lib.harness.projections import project_pi_rpc as _project_pi_rpc

    from meridian.lib.harness.extractors import claude as _claude_extractor
    from meridian.lib.harness.extractors import codex as _codex_extractor
    from meridian.lib.harness.extractors import cursor as _cursor_extractor
    from meridian.lib.harness.extractors import opencode as _opencode_extractor
    from meridian.lib.harness.extractors import pi as _pi_extractor

    from meridian.lib.harness.launch_spec import _enforce_spawn_params_accounting

    _bootstrap_imports = (
        _claude,
        _codex,
        _cursor,
        _opencode,
        _pi,
        _project_claude,
        _project_cursor,
        _project_codex_subprocess,
        _project_codex_streaming,
        _project_opencode_subprocess,
        _project_opencode_streaming,
        _project_pi_rpc,
        _claude_extractor,
        _codex_extractor,
        _cursor_extractor,
        _opencode_extractor,
        _pi_extractor,
    )
    _enforce_spawn_params_accounting()
    _bootstrapped = True


def ensure_bootstrap() -> None:
    """Ensure bundle/projection bootstrap has completed."""

    if _bootstrapped:
        return
    _run_bootstrap()


_run_bootstrap()

__all__ = ["HARNESS_EXTENSION_TOUCHPOINTS", "ensure_bootstrap"]
