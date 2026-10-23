"""Import-cheap delegation boundary carried from a harness into child launches."""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from dataclasses import dataclass
from typing import cast

DELEGATION_FLOOR_ENV = "_MERIDIAN_DELEGATION_FLOOR"

_SANDBOX_WEAKNESS = {
    "read-only": 0,
    "workspace-write": 1,
    "danger-full-access": 2,
    # A harness default is unknown, so compare it as weaker than every explicit mode.
    "default": 3,
}
_APPROVAL_STRENGTH = {
    "never": 0,
    "yolo": 0,
    "auto": 1,
    "default": 2,
    "confirm": 3,
}
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
        "OPENCODE_DB",
        "OPENCODE_HOME",
        "PI_CODING_AGENT_DIR",
        "PI_CODING_AGENT_SESSION_DIR",
    }
)


@dataclass(frozen=True)
class DelegationFloor:
    """The confinement floor of the process that may request a child launch."""

    sandbox: str
    approval: str
    has_spawn_capability: bool

    @classmethod
    def from_policy(
        cls,
        *,
        sandbox: str | None,
        approval: str | None,
        has_spawn_capability: bool,
    ) -> DelegationFloor:
        resolved_sandbox = (sandbox or "default").strip().lower()
        resolved_approval = (approval or "default").strip().lower()
        if resolved_sandbox not in _SANDBOX_WEAKNESS:
            raise ValueError(f"Unsupported sandbox mode '{sandbox}'.")
        if resolved_approval not in _APPROVAL_STRENGTH:
            raise ValueError(f"Unsupported approval mode '{approval}'.")
        if resolved_approval == "yolo":
            resolved_approval = "never"
        return cls(
            sandbox=resolved_sandbox,
            approval=resolved_approval,
            has_spawn_capability=has_spawn_capability,
        )

    def to_env_value(self) -> str:
        """Serialize a deterministic launcher-owned transport value."""

        return json.dumps(
            {
                "sandbox": self.sandbox,
                "approval": self.approval,
                "spawn": self.has_spawn_capability,
            },
            separators=(",", ":"),
            sort_keys=True,
        )

    @classmethod
    def from_env_value(cls, raw: str) -> DelegationFloor:
        """Parse the launcher transport, refusing malformed or incomplete values."""

        try:
            decoded: object = json.loads(raw)
        except (json.JSONDecodeError, TypeError) as exc:
            raise ValueError("Child spawn refused: launcher delegation floor is invalid.") from exc
        if not isinstance(decoded, dict):
            raise ValueError("Child spawn refused: launcher delegation floor is invalid.")
        payload = cast("dict[object, object]", decoded)
        if set(payload) != {"sandbox", "approval", "spawn"}:
            raise ValueError("Child spawn refused: launcher delegation floor is invalid.")
        sandbox = payload.get("sandbox")
        approval = payload.get("approval")
        spawn = payload.get("spawn")
        if (
            not isinstance(sandbox, str)
            or not isinstance(approval, str)
            or not isinstance(spawn, bool)
        ):
            raise ValueError("Child spawn refused: launcher delegation floor is invalid.")
        try:
            return cls.from_policy(
                sandbox=sandbox,
                approval=approval,
                has_spawn_capability=spawn,
            )
        except ValueError as exc:
            raise ValueError("Child spawn refused: launcher delegation floor is invalid.") from exc


@dataclass(frozen=True)
class DelegationFloorCheck:
    """One launch check, split around policy resolution to avoid needless Mars work."""

    caller: DelegationFloor | None

    @classmethod
    def from_environment(cls, requested_env: Iterable[str]) -> DelegationFloorCheck:
        raw_floor = os.environ.get(DELEGATION_FLOOR_ENV)
        if raw_floor is None:
            return cls(caller=None)
        caller = DelegationFloor.from_env_value(raw_floor)
        if not caller.has_spawn_capability:
            raise ValueError(
                "Child spawn refused: caller profile has no spawn capability."
            )
        check = cls(caller=caller)
        check.enforce_env_keys(assignment.partition("=")[0] for assignment in requested_env)
        return check

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
                    "unsandboxed process loader, executable, or harness home."
                )

    def enforce(self, child: DelegationFloor) -> None:
        """Refuse a resolved child policy that is weaker than the caller floor."""

        caller = self.caller
        if caller is None:
            return
        if _SANDBOX_WEAKNESS[child.sandbox] > _SANDBOX_WEAKNESS[caller.sandbox]:
            raise ValueError(
                f"Child spawn refused: sandbox '{child.sandbox}' is weaker than "
                f"caller floor '{caller.sandbox}'."
            )
        if _APPROVAL_STRENGTH[child.approval] < _APPROVAL_STRENGTH[caller.approval]:
            raise ValueError(
                f"Child spawn refused: approval '{child.approval}' is weaker than "
                f"caller floor '{caller.approval}'."
            )
__all__ = [
    "DELEGATION_FLOOR_ENV",
    "DelegationFloor",
    "DelegationFloorCheck",
]
