"""Launcher-recorded CLI-escalation guardrail for child spawn creation."""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from meridian.lib.safety.policy import (
    CONSTRAINED_SANDBOX_MODES,
    SANDBOX_MODE_WEAKNESS,
    NetworkMode,
    SandboxMode,
    parse_network_mode,
)
from meridian.lib.utils.json_shapes import loads_json_object

if TYPE_CHECKING:
    from meridian.lib.safety.policy import PermissionPolicy

CALLER_BOUNDARY_ENV = "_MERIDIAN_CALLER_BOUNDARY"
SANDBOXED_SPAWN_LAUNCH_ERROR = (
    "Cannot launch a child spawn from inside the parent shell sandbox. "
    "Run `meridian spawn` as a standalone shell command: no `cd ... &&`, pipes, `$()`, "
    "or `VAR=1` prefix."
)

_PID_NAMESPACE_PATH = "/proc/self/ns/pid"


def current_pid_namespace() -> str | None:
    """Return this process's PID-namespace identity when procfs exposes it."""

    try:
        return os.readlink(_PID_NAMESPACE_PATH)
    except OSError:
        return None


@dataclass(frozen=True)
class CallerBoundary:
    """Guardrail facts recorded by the process that launched a harness."""

    pid_namespace: str | None
    sandbox: SandboxMode
    network_deny_floor: bool
    has_spawn_capability: bool

    @classmethod
    def from_policy(
        cls,
        policy: PermissionPolicy,
        *,
        effective_network: NetworkMode,
        pid_namespace: str | None,
    ) -> CallerBoundary:
        """Take the child-launch boundary fields from one resolved policy."""

        sandbox = policy.sandbox
        return cls(
            pid_namespace=pid_namespace,
            sandbox=sandbox,
            network_deny_floor=(
                policy.network == "deny" and effective_network == "deny"
            ),
            has_spawn_capability=policy.has_spawn_capability,
        )

    @property
    def is_floored(self) -> bool:
        """Whether Meridian projected the sandbox this command escapes."""

        return self.sandbox in CONSTRAINED_SANDBOX_MODES

    def to_env_value(self) -> str:
        """Serialize the complete launcher-owned record deterministically."""

        payload: dict[str, object] = {
            "pid_namespace": self.pid_namespace,
            "sandbox": self.sandbox,
            "spawn": self.has_spawn_capability,
        }
        if self.network_deny_floor:
            payload["network"] = "deny"
        return json.dumps(
            payload,
            separators=(",", ":"),
            sort_keys=True,
        )

    @classmethod
    def from_env_value(cls, raw: str) -> CallerBoundary:
        """Parse known launcher facts while tolerating fields from newer builds."""

        payload = loads_json_object(raw)
        if payload is None:
            raise ValueError("Child spawn refused: caller boundary is invalid.")
        if "pid_namespace" not in payload or "sandbox" not in payload:
            raise ValueError("Child spawn refused: caller boundary is invalid.")
        pid_namespace = payload.get("pid_namespace")
        sandbox = payload.get("sandbox")
        network = payload.get("network")
        spawn = payload.get("spawn", False)
        if (
            (pid_namespace is not None and not isinstance(pid_namespace, str))
            or not isinstance(sandbox, str)
            or (network is not None and not isinstance(network, str))
            or not isinstance(spawn, bool)
            or sandbox not in SANDBOX_MODE_WEAKNESS
        ):
            raise ValueError("Child spawn refused: caller boundary is invalid.")
        try:
            parsed_network = parse_network_mode(
                network if isinstance(network, str) else None
            )
        except ValueError as error:
            raise ValueError(
                "Child spawn refused: caller boundary is invalid."
            ) from error
        return cls(
            pid_namespace=pid_namespace,
            sandbox=sandbox,
            # Older builds wrote "allow" explicitly. It carried no restrictive floor.
            network_deny_floor=parsed_network == "deny",
            has_spawn_capability=spawn,
        )


@dataclass(frozen=True)
class CallerBoundaryCheck:
    """One caller-boundary check split around child policy resolution."""

    caller: CallerBoundary | None

    @classmethod
    def from_environment(
        cls,
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
        check.enforce_passthrough_args(passthrough_args)
        return check

    def enforce_passthrough_args(self, passthrough_args: Iterable[str]) -> None:
        """Reject native argv that could override Meridian's resolved policy."""

        if self.caller is not None and any(True for _ in passthrough_args):
            raise ValueError(
                "Child spawn refused: harness passthrough arguments can override the "
                "unsandboxed child policy."
            )

    def enforce(
        self,
        child_sandbox: str | None,
        *,
        child_network: NetworkMode,
        child_declared_network: NetworkMode | None,
    ) -> None:
        """Refuse a resolved child policy weaker than the caller's guardrail."""

        caller = self.caller
        if caller is None:
            return
        normalized = (child_sandbox or "default").strip().lower()
        if normalized not in SANDBOX_MODE_WEAKNESS:
            raise ValueError(f"Unsupported sandbox mode '{child_sandbox}'.")
        if SANDBOX_MODE_WEAKNESS[normalized] > SANDBOX_MODE_WEAKNESS[caller.sandbox]:
            raise ValueError(
                f"Child spawn refused: sandbox '{normalized}' is weaker than "
                f"caller floor '{caller.sandbox}'."
            )
        if caller.network_deny_floor and child_network != "deny":
            remedy = (
                "the child declares `network: deny`, but its harness or native override "
                "does not enforce it"
                if child_declared_network == "deny"
                else "set `network: deny` on the child profile"
            )
            raise ValueError(
                f"Child spawn refused: effective network '{child_network}' is weaker than "
                f"caller floor 'deny'; {remedy}."
            )


__all__ = [
    "CALLER_BOUNDARY_ENV",
    "SANDBOXED_SPAWN_LAUNCH_ERROR",
    "CallerBoundary",
    "CallerBoundaryCheck",
    "current_pid_namespace",
]
