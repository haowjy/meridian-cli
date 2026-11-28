from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable

from meridian.lib.extensions.declaration import OpDeclaration
from meridian.lib.extensions.types import ExtensionCommandSpec, ExtensionSurface


class ExtensionCommandRegistry:
    """Registry of extension commands with validation.

    Declarations are validated from metadata and import their implementation only when a
    caller needs the full spec (``get``, ``list_all``, ``list_for_surface``).
    """

    def __init__(self) -> None:
        self._commands: dict[str, OpDeclaration] = {}
        self._cli_commands: dict[tuple[str, str], str] = {}  # (group, name) -> fqid

    def register(self, declaration: OpDeclaration) -> None:

        fqid = declaration.fqid
        if fqid in self._commands:
            raise ValueError(f"Duplicate extension command: {fqid}")

        cli_key: tuple[str, str] | None = None
        if declaration.cli_group is not None and declaration.cli_name is not None:
            cli_key = (declaration.cli_group, declaration.cli_name)
            if cli_key in self._cli_commands:
                raise ValueError(
                    f"Duplicate CLI command '{declaration.cli_group}.{declaration.cli_name}': "
                    f"{fqid} conflicts with {self._cli_commands[cli_key]}"
                )

        self._commands[fqid] = declaration
        if cli_key is not None:
            self._cli_commands[cli_key] = fqid

    def get(self, fqid: str) -> ExtensionCommandSpec | None:
        declaration = self._commands.get(fqid)
        return declaration.resolved if declaration is not None else None

    def list_for_cli_group(self, group: str) -> list[OpDeclaration]:
        return sorted(
            (d for d in self._commands.values() if d.cli_group == group),
            key=lambda d: d.cli_name or "",
        )

    def list_all(self) -> list[ExtensionCommandSpec]:
        return [declaration.resolved for declaration in self._commands.values()]

    def list_for_surface(self, surface: ExtensionSurface) -> list[ExtensionCommandSpec]:

        return [d.resolved for d in self._commands.values() if surface in d.surfaces]

    def __len__(self) -> int:
        return len(self._commands)

    def __iter__(self) -> Iterable[ExtensionCommandSpec]:
        return iter(self.list_all())


_command_registry: ExtensionCommandRegistry | None = None


def get_command_registry() -> ExtensionCommandRegistry:
    """Return the process-wide registry of every operation in ``ops/commands.py``."""

    global _command_registry
    if _command_registry is None:
        from meridian.lib.ops.commands import OP_DECLARATIONS

        registry = ExtensionCommandRegistry()
        for declaration in OP_DECLARATIONS:
            registry.register(declaration)
        _command_registry = registry
    return _command_registry


def compute_manifest_hash(registry: ExtensionCommandRegistry) -> str:
    """Compute deterministic hash of all registered commands.

    EB1.12: Same registry in-process and subprocess yields identical hash.
    Includes input/output schemas so schema changes rotate the hash.
    """

    specs = sorted(registry.list_all(), key=lambda s: s.fqid)
    manifest_data = [
        {
            "fqid": spec.fqid,
            "summary": spec.summary,
            "surfaces": sorted(surface.value for surface in spec.surfaces),
            # Every command is first-party; the key stays so the published hash is unchanged.
            "first_party": True,
            "required_capabilities": sorted(spec.required_capabilities),
            "args_schema": spec.args_schema.model_json_schema(),
            "result_schema": spec.result_schema.model_json_schema(),
        }
        for spec in specs
    ]
    payload = json.dumps(manifest_data, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


__all__ = [
    "ExtensionCommandRegistry",
    "compute_manifest_hash",
    "get_command_registry",
]
