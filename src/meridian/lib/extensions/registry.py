"""Extension command registry and first-party registry helpers."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable

from meridian.lib.extensions.declaration import OpDeclaration
from meridian.lib.extensions.types import ExtensionCommandSpec, ExtensionSurface

# Both expose the registration metadata (fqid, summary, cli_group, cli_name, surfaces,
# first_party); only a resolved spec carries handlers and schemas.
type CommandEntry = ExtensionCommandSpec | OpDeclaration


def _resolved(entry: CommandEntry) -> ExtensionCommandSpec:
    return entry.resolved if isinstance(entry, OpDeclaration) else entry


class ExtensionCommandRegistry:
    """Registry of extension commands with validation.

    Declarations are validated from metadata and import their implementation only when a
    caller needs the full spec (``get``, ``list_all``, ``list_for_surface``).
    """

    def __init__(self) -> None:
        self._commands: dict[str, CommandEntry] = {}
        self._cli_commands: dict[tuple[str, str], str] = {}  # (group, name) -> fqid

    def register(self, spec: CommandEntry) -> None:
        """Register a command spec or declaration. Raises ValueError on validation failure."""

        fqid = spec.fqid
        if fqid in self._commands:
            raise ValueError(f"Duplicate extension command: {fqid}")

        cli_key: tuple[str, str] | None = None
        if spec.cli_group is not None and spec.cli_name is not None:
            cli_key = (spec.cli_group, spec.cli_name)
            if cli_key in self._cli_commands:
                raise ValueError(
                    f"Duplicate CLI command '{spec.cli_group}.{spec.cli_name}': "
                    f"{fqid} conflicts with {self._cli_commands[cli_key]}"
                )

        if not spec.first_party:
            forbidden_surfaces = {ExtensionSurface.CLI, ExtensionSurface.MCP}
            if spec.surfaces & forbidden_surfaces:
                raise ValueError(
                    f"Non-first-party command {fqid} cannot expose CLI or MCP surfaces",
                )

        self._commands[fqid] = spec
        if cli_key is not None:
            self._cli_commands[cli_key] = fqid

    def get(self, fqid: str) -> ExtensionCommandSpec | None:
        entry = self._commands.get(fqid)
        return _resolved(entry) if entry is not None else None

    def list_for_cli_group(self, group: str) -> list[CommandEntry]:
        """Return a CLI group's entries, sorted by cli_name, without resolving them."""
        return sorted(
            (s for s in self._commands.values() if s.cli_group == group),
            key=lambda s: s.cli_name or "",
        )

    def list_all(self) -> list[ExtensionCommandSpec]:
        return [_resolved(entry) for entry in self._commands.values()]

    def list_for_surface(self, surface: ExtensionSurface) -> list[ExtensionCommandSpec]:
        """Return commands available on a specific surface."""

        return [_resolved(e) for e in self._commands.values() if surface in e.surfaces]

    def __len__(self) -> int:
        return len(self._commands)

    def __iter__(self) -> Iterable[ExtensionCommandSpec]:
        return iter(self.list_all())


_first_party_registry: ExtensionCommandRegistry | None = None


def get_first_party_registry() -> ExtensionCommandRegistry:
    """Return the singleton first-party registry."""

    global _first_party_registry
    if _first_party_registry is None:
        _first_party_registry = build_first_party_registry()
    return _first_party_registry


def build_first_party_registry() -> ExtensionCommandRegistry:
    """Build a fresh registry with first-party commands. For testing."""

    from meridian.lib.extensions.first_party import register_first_party_commands

    registry = ExtensionCommandRegistry()
    register_first_party_commands(registry)
    return registry


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
            "first_party": spec.first_party,
            "required_capabilities": sorted(spec.required_capabilities),
            "args_schema": spec.args_schema.model_json_schema(),
            "result_schema": spec.result_schema.model_json_schema(),
        }
        for spec in specs
    ]
    payload = json.dumps(manifest_data, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


__all__ = [
    "CommandEntry",
    "ExtensionCommandRegistry",
    "build_first_party_registry",
    "compute_manifest_hash",
    "get_first_party_registry",
]
