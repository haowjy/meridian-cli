"""First-party extension command registrations."""

from meridian.lib.extensions.registry import ExtensionCommandRegistry


def register_first_party_commands(registry: ExtensionCommandRegistry) -> None:
    """Register all v1 first-party commands."""

    from meridian.lib.extensions.commands.mermaid import MERMAID_CHECK_SPEC
    from meridian.lib.ops.commands import get_all_op_specs

    registry.register(MERMAID_CHECK_SPEC)
    for spec in get_all_op_specs():
        registry.register(spec)
