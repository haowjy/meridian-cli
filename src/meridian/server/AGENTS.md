# MCP server

Stdout is the MCP transport; unrelated output corrupts it. Add commands to
`_IDENTITY_WRITING_TOOLS` only when they need to mint identity, since the other
commands bootstrap read-only.
