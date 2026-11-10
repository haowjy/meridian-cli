# Extension commands

The CLI and the MCP server are Meridian's two command surfaces. They use one spec per
command so their validation cannot diverge, and each command declares which surfaces
expose it. There is no HTTP server surface.

The capability and trust checks form the seam for future third-party extensions;
current first-party commands do not exercise capability grants. Keep the seam: it is
planned work, not dead code.
