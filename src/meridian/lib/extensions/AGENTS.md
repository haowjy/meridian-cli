# Extension commands

Return a specific `ExtensionErrorResult` from handlers; raised exceptions use
the generic `handler_error` code. CLI registration reads unresolved
metadata to avoid importing all operation handlers at startup.

Every handler is synchronous and `dispatch()` runs it in a worker thread; an
operation with async work runs its own loop inside the handler.

[.context/CONTEXT.md](.context/CONTEXT.md) describes the capability seam.
