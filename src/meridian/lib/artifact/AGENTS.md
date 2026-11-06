# Artifact serving

Tailscale teardown removes only the recorded slug: disabling a port or resetting
serve would drop unrelated publishes. Validate slugs before invoking Tailscale;
empty slugs can target the root handler.

[.context/CONTEXT.md](.context/CONTEXT.md) explains the port and path choices.
