# ruff: noqa: I001

from __future__ import annotations

import threading

_bootstrapped = False
_bootstrap_imports: tuple[object, ...] = ()
_bootstrap_lock = threading.RLock()

HARNESS_EXTENSION_TOUCHPOINTS: tuple[str, ...] = (
    "src/meridian/lib/core/types.py (HarnessId member; help text and shortcuts derive from it)",
    (
        "src/meridian/lib/harness/<new_harness>.py "
        "(adapter, its HarnessContract and handled_fields, and the register_harness() "
        "call: LaunchPorts bind adapter, spec class, projection ports and semantics; "
        "ProcessPorts bind extractor, transport connections and idle, route and event ports)"
    ),
    (
        "src/meridian/lib/harness/__init__.py::_run_bootstrap() "
        "(import the adapter, projection and extractor modules so they register)"
    ),
    (
        "src/meridian/lib/harness/registry.py::HarnessRegistry.with_defaults() "
        "(import and register the adapter instance)"
    ),
    (
        "src/meridian/lib/harness/projections/project_<new_harness>*.py "
        "(spec to argv/env/transport payloads, with import-time drift guards)"
    ),
    (
        "src/meridian/lib/harness/projections/permissions_<new_harness>.py "
        "(the compile of the permission policy and its enforcement report; register the "
        "report and effective-network function as HarnessProjectionPorts)"
    ),
    ("src/meridian/lib/harness/extractors/<new_harness>.py (report, session and usage extraction)"),
    (
        "src/meridian/lib/harness/connections/<new_harness>_<transport>.py "
        "(streaming transport; only when the bundle declares one)"
    ),
    (
        "HarnessContract.workspace_roots, set in the adapter "
        "(ADD_DIR_ARGS, ENV, or the default NONE; `config show` lists the harnesses that "
        "take roots)"
    ),
    (
        "adapter read_last_model() and native_session_evidence() "
        "(override to read the model from, and to repair, a native session file; the "
        "defaults read nothing and refuse `session repair --native`)"
    ),
    (
        "src/meridian/lib/config/harness_models.py::HarnessConfig "
        "(one field and profile model per harness; without it a `[harness.<id>]` table is "
        "dropped silently and harness_profile() returns None)"
    ),
    (
        "src/meridian/lib/launch/context.py (the `harness.id ==` branches that bind "
        "launch-artifact paths into the spec: system prompt file, settings file, report "
        "output path; add one when the harness's spec takes them)"
    ),
    (
        "src/meridian/lib/ops/session_repair.py::_candidate_dirs() and "
        "ops/session_search_index.py (optional: candidate listing without --native, and a "
        "store shared by many sessions, as OpenCode's database is)"
    ),
    (
        "src/meridian/lib/harness/adapter.py::create_session_before_exec() "
        "(override only when Meridian must create the native session before exec, on a "
        "fresh launch or a fork, never a resume; an assign_session_id() override "
        "must then return LaunchIntent.precreated_session_id; "
        "HarnessPrelaunchState.route_payload hands in-memory facts to "
        "ProcessPorts.primary_message_route)"
    ),
)


def _run_bootstrap() -> None:
    global _bootstrapped
    global _bootstrap_imports

    # Import order is load-bearing.
    #
    # 1) Adapter modules register LaunchPorts and ProcessPorts as module-load side effects.
    # 2) Projection modules execute import-time drift guards.
    # 3) Extractor modules bind runtime-checkable Protocol implementations.
    # 4) Cross-adapter SpawnParams accounting runs after all registrations.
    from meridian.lib.harness.agy import adapter as _agy
    from meridian.lib.harness import claude as _claude
    from meridian.lib.harness import codex as _codex
    from meridian.lib.harness import opencode as _opencode
    from meridian.lib.harness import pi as _pi

    from meridian.lib.harness.projections import project_claude as _project_claude
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
    from meridian.lib.harness.extractors import opencode as _opencode_extractor
    from meridian.lib.harness.extractors import pi as _pi_extractor

    from meridian.lib.harness.bundle import registered_launch_ports
    from meridian.lib.harness.launch_spec import _enforce_spawn_params_accounting

    _bootstrap_imports = (
        _agy,
        _claude,
        _codex,
        _opencode,
        _pi,
        _project_claude,
        _project_codex_subprocess,
        _project_codex_streaming,
        _project_opencode_subprocess,
        _project_opencode_streaming,
        _project_pi_rpc,
        _claude_extractor,
        _codex_extractor,
        _opencode_extractor,
        _pi_extractor,
    )
    # The public registry accessor would re-enter ensure_bootstrap.
    _enforce_spawn_params_accounting(registered_launch_ports())
    _bootstrapped = True


def ensure_bootstrap() -> None:

    if _bootstrapped:
        return
    with _bootstrap_lock:
        if _bootstrapped:
            return
        _run_bootstrap()


__all__ = ["HARNESS_EXTENSION_TOUCHPOINTS", "ensure_bootstrap"]
