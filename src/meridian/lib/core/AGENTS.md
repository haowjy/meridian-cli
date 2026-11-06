# Core

`RuntimeOverrides` uses `None` for an unset layer. An empty string or zero is a
value and wins over lower-precedence layers.

Keep the lazy `spawn_store` import in `lifecycle.py`; an eager import creates a
cycle with state initialization. [.context/CONTEXT.md](.context/CONTEXT.md)
covers lifecycle and cleanup contracts.
