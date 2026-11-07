from meridian.lib.extensions.registry import ExtensionCommandRegistry


def register_first_party_commands(registry: ExtensionCommandRegistry) -> None:

    from meridian.lib.extensions.commands.mermaid import MERMAID_CHECK_SPEC
    from meridian.lib.ops.commands import OP_DECLARATIONS

    registry.register(MERMAID_CHECK_SPEC)
    for declaration in OP_DECLARATIONS:
        registry.register(declaration)
