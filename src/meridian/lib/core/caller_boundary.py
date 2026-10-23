"""Launcher-recorded confinement boundary for child spawn creation."""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from meridian.lib.safety.policy import (
    CONSTRAINED_SANDBOX_MODES,
    SANDBOX_MODE_WEAKNESS,
    SandboxMode,
)
from meridian.lib.utils.json_shapes import json_list, loads_json_object

if TYPE_CHECKING:
    from meridian.lib.safety.policy import PermissionPolicy

CALLER_BOUNDARY_ENV = "_MERIDIAN_CALLER_BOUNDARY"
SANDBOXED_SPAWN_LAUNCH_ERROR = (
    "Cannot launch a child spawn from inside the parent shell sandbox. "
    "Run `meridian spawn` as a standalone shell command: no `cd ... &&`, pipes, `$()`, "
    "or `VAR=1` prefix."
)

_PID_NAMESPACE_PATH = "/proc/self/ns/pid"
_FORBIDDEN_ENV_KEYS = frozenset(
    {
        "PATH",
        "HOME",
        "BASH_ENV",
        "ENV",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "NODE_OPTIONS",
        "NODE_PATH",
        "PYTHONHOME",
        "PYTHONPATH",
        "CODEX_HOME",
        "CLAUDE_CONFIG_DIR",
        "OPENCODE_CONFIG",
        "OPENCODE_CLI_CONFIG_CONTENT",
        "OPENCODE_CONFIG_CONTENT",
        "OPENCODE_CONFIG_DIR",
        "OPENCODE_DB",
        "OPENCODE_HOME",
        "PI_CODING_AGENT_DIR",
        "PI_CODING_AGENT_SESSION_DIR",
    }
)


def current_pid_namespace() -> str | None:
    """Return this process's PID-namespace identity when procfs exposes it."""

    try:
        return os.readlink(_PID_NAMESPACE_PATH)
    except OSError:
        return None


@dataclass(frozen=True)
class CallerBoundary:
    """Confinement facts recorded by the process that launched a harness."""

    pid_namespace: str | None
    sandbox: SandboxMode
    write_roots: tuple[str, ...]
    has_spawn_capability: bool

    @classmethod
    def from_policy(
        cls,
        policy: PermissionPolicy,
        *,
        pid_namespace: str | None,
    ) -> CallerBoundary:
        """Take the child-launch boundary fields from one resolved policy."""

        sandbox = policy.sandbox
        return cls(
            pid_namespace=pid_namespace,
            sandbox=sandbox,
            write_roots=policy.sandbox_write_roots,
            has_spawn_capability=policy.has_spawn_capability,
        )

    @classmethod
    def from_values(
        cls,
        *,
        sandbox: str | None,
        has_spawn_capability: bool,
        pid_namespace: str | None = None,
        write_roots: tuple[str, ...] = (),
    ) -> CallerBoundary:
        """Build a boundary before the complete permission policy is bound."""

        normalized_sandbox = (sandbox or "default").strip().lower()
        if normalized_sandbox not in SANDBOX_MODE_WEAKNESS:
            raise ValueError(f"Unsupported sandbox mode '{sandbox}'.")
        return cls(
            pid_namespace=pid_namespace,
            sandbox=normalized_sandbox,
            write_roots=write_roots,
            has_spawn_capability=has_spawn_capability,
        )

    @property
    def is_floored(self) -> bool:
        """Whether Meridian projected the sandbox this command escapes."""

        return self.sandbox in CONSTRAINED_SANDBOX_MODES

    def to_env_value(self) -> str:
        """Serialize the complete launcher-owned record deterministically."""

        return json.dumps(
            {
                "pid_namespace": self.pid_namespace,
                "sandbox": self.sandbox,
                "spawn": self.has_spawn_capability,
                "write_roots": self.write_roots,
            },
            separators=(",", ":"),
            sort_keys=True,
        )

    @classmethod
    def from_env_value(cls, raw: str) -> CallerBoundary:
        """Parse the launcher transport, refusing malformed or incomplete values."""

        payload = loads_json_object(raw)
        if payload is None:
            raise ValueError("Child spawn refused: caller boundary is invalid.")
        if set(payload) != {
            "pid_namespace",
            "sandbox",
            "spawn",
            "write_roots",
        }:
            raise ValueError("Child spawn refused: caller boundary is invalid.")
        pid_namespace = payload.get("pid_namespace")
        sandbox = payload.get("sandbox")
        spawn = payload.get("spawn")
        write_root_values = json_list(payload.get("write_roots"))
        write_roots = (
            tuple(root for root in write_root_values if isinstance(root, str))
            if write_root_values is not None
            else ()
        )
        if (
            (pid_namespace is not None and not isinstance(pid_namespace, str))
            or not isinstance(sandbox, str)
            or not isinstance(spawn, bool)
            or write_root_values is None
            or len(write_roots) != len(write_root_values)
            or sandbox not in SANDBOX_MODE_WEAKNESS
        ):
            raise ValueError("Child spawn refused: caller boundary is invalid.")
        return cls(
            pid_namespace=pid_namespace,
            sandbox=sandbox,
            write_roots=write_roots,
            has_spawn_capability=spawn,
        )


@dataclass(frozen=True)
class CallerBoundaryCheck:
    """One caller-boundary check split around child policy resolution."""

    caller: CallerBoundary | None

    @classmethod
    def from_environment(
        cls,
        requested_env: Iterable[str],
        *,
        dry_run: bool,
        passthrough_args: Iterable[str] = (),
    ) -> CallerBoundaryCheck:
        """Check launcher facts before operation-specific state access."""

        raw_boundary = os.environ.get(CALLER_BOUNDARY_ENV)
        if raw_boundary is None:
            return cls(caller=None)
        caller = CallerBoundary.from_env_value(raw_boundary)
        if not dry_run and caller.pid_namespace:
            namespace = current_pid_namespace()
            if namespace is not None and namespace != caller.pid_namespace:
                raise ValueError(SANDBOXED_SPAWN_LAUNCH_ERROR)
        if not caller.is_floored:
            return cls(caller=None)
        if not caller.has_spawn_capability:
            raise ValueError(
                "Child spawn refused: caller profile has no spawn capability."
            )
        check = cls(caller=caller)
        check.enforce_env_keys(assignment.partition("=")[0] for assignment in requested_env)
        check.enforce_passthrough_args(passthrough_args)
        return check

    def enforce_passthrough_args(self, passthrough_args: Iterable[str]) -> None:
        """Reject native argv that could override Meridian's resolved policy."""

        if self.caller is None:
            return
        for _arg in passthrough_args:
            raise ValueError(
                "Child spawn refused: harness passthrough arguments can override the "
                "unsandboxed child policy."
            )

    def enforce_env_keys(self, keys: Iterable[str]) -> None:
        """Reject environment keys that can escape the resolved launch namespace."""

        if self.caller is None:
            return
        for key in keys:
            key = key.strip()
            normalized = key.upper()
            if (
                normalized in _FORBIDDEN_ENV_KEYS
                or normalized.startswith("LD_")
                or normalized.startswith("DYLD_")
            ):
                raise ValueError(
                    f"Child spawn refused: environment key '{key}' can change the "
                    "unsandboxed process or harness config loader, executable, or "
                    "harness home."
                )

    def enforce(self, child: CallerBoundary) -> None:
        """Refuse a resolved child policy that is weaker than the caller boundary."""

        caller = self.caller
        if caller is None:
            return
        if SANDBOX_MODE_WEAKNESS[child.sandbox] > SANDBOX_MODE_WEAKNESS[caller.sandbox]:
            raise ValueError(
                f"Child spawn refused: sandbox '{child.sandbox}' is weaker than "
                f"caller floor '{caller.sandbox}'."
            )
        caller_roots = tuple(Path(root).resolve() for root in caller.write_roots)
        for child_root_value in child.write_roots:
            child_root = Path(child_root_value).resolve()
            if not any(
                child_root == root or child_root.is_relative_to(root) for root in caller_roots
            ):
                raise ValueError(
                    f"Child spawn refused: workspace root '{child_root}' is outside "
                    "the caller's projected workspace roots."
                )


__all__ = [
    "CALLER_BOUNDARY_ENV",
    "SANDBOXED_SPAWN_LAUNCH_ERROR",
    "CallerBoundary",
    "CallerBoundaryCheck",
    "current_pid_namespace",
]
