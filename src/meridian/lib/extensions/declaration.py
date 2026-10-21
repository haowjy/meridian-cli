"""Lightweight operation declarations; implementations load only when needed."""

from dataclasses import dataclass
from functools import cached_property
from importlib import import_module

from meridian.lib.extensions.types import ExtensionCommandSpec, ExtensionSurface


def _binding(address: str):
    module, name = address.split(":", 1)
    return getattr(import_module(module), name)


@dataclass(frozen=True)
class OpDeclaration:
    extension_id: str
    command_id: str
    summary: str
    handler: str
    sync_handler: str | None
    input_type: str
    output_type: str
    cli_group: str | None = None
    cli_name: str | None = None
    surfaces: frozenset[ExtensionSurface] = frozenset({ExtensionSurface.CLI, ExtensionSurface.MCP})
    first_party: bool = True

    @property
    def fqid(self) -> str:
        return f"{self.extension_id}.{self.command_id}"

    @cached_property
    def resolved(self) -> ExtensionCommandSpec:
        """Import the implementation and build the full spec (once)."""
        return ExtensionCommandSpec.from_op(
            extension_id=self.extension_id,
            command_id=self.command_id,
            summary=self.summary,
            handler=_binding(self.handler),
            sync_handler=_binding(self.sync_handler) if self.sync_handler else None,
            input_type=_binding(self.input_type),
            output_type=_binding(self.output_type),
            cli_group=self.cli_group,
            cli_name=self.cli_name,
            surfaces=self.surfaces,
            first_party=self.first_party,
        )
