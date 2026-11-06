# Extension commands

Return a specific `ExtensionErrorResult` from handlers; raised exceptions are
reduced to `handler_error` and lose context. CLI registration reads unresolved
metadata to avoid importing all operation handlers at startup.

[.context/CONTEXT.md](.context/CONTEXT.md) describes the capability seam.
